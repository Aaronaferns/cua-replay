"""Replay v0: run a capability against a surface with no model in the loop.

A capability is a guarded linear program over classified screen states. For each step the interpreter acts on one
target (exactly-one resolution), then waits for the screen to settle in a state the step allows: the next step's
expected state, a declared branch, or a state the profile gives a default meaning. Anything else is a failure with
the observed state and its near misses; replay never improvises, never retries a commit and never calls a model.

Injected (nothing here knows how to build any of them):

    surface   a `cua.surface.base.Surface`; every action goes through its guard-checked methods
    guard     a `Guard`, consulted here only for reads (`extract`), which the surface has no method for
    lease     returns the current `LeaseToken`; asked per action so a transfer is noticed
    evidence  optional `EvidenceWriter`; events carry tokens and state names, never input or output values

v0 covers: input validation, scripted login from the profile, the happy path, declared branches and default
meanings, identity asserts, value-echo verifies, success checks, typed output parsing, drift, policy denial as a
failure with category `policy`. The full failure taxonomy, handlers, human handoff and commit confirmation arrive
with M4 to M6 and plug in at `_settle` (classification and acceptance) and `_failure` (categorization).

Imports: artifact, surface, safety, control, evidence and this package. Never agent, compiler.recorder, faults or a
model SDK (`tests/replay/test_replay_imports.py` enforces it).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from cua.artifact.classify import Classification
from cua.artifact.lock import lock_hash
from cua.artifact.predicates import Predicate
from cua.artifact.schema import (
    TEMPLATE_RE,
    AssertAction,
    Branch,
    Capability,
    ClickAction,
    ExtractAction,
    NavigateAction,
    PressAction,
    Profile,
    SelectAction,
    Step,
    Target,
    TypeAction,
)
from cua.artifact.values import ValueRejected, check_input, parse_output
from cua.control.lease import LeaseToken
from cua.evidence.events import EvidenceEvent, EvidenceWriter
from cua.replay.checks import evaluate
from cua.replay.classifier import classify_observation, mask_url, views_of
from cua.replay.resolver import ResolutionError, Resolved, resolve
from cua.replay.result import (
    CommitState,
    DriftWarning,
    Failure,
    ObservedState,
    Recovery,
    RunResult,
    SideEffects,
)
from cua.safety.policy import ActionKind, ActionRequest, Actor, Guard, PolicyDecision, PolicyDenied
from cua.surface.base import NativeDialogOpen, NotUniqueError, Observation, Surface, SurfaceError

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class Credentials:
    """Login credentials for the profile's scripted login. The password never appears in a repr."""

    username: str
    password: str = field(repr=False)


@dataclass(frozen=True)
class Timing:
    poll_interval_ms: int = 150  # settled = the same verdict on `stable_polls` polls this far apart
    stable_polls: int = 2  # applied to steps that navigate; other steps need one poll
    entry_timeout_ms: int = 15_000


@dataclass(frozen=True)
class _Verdict:
    kind: str  # "continue" | "login" | "outcome" | "failure"
    key: str  # what must be stable across polls
    code: str | None = None  # outcome or failure code
    state: str | None = None


class _Stop(Exception):
    """Ends the run early with a terminal result."""

    def __init__(self, status: str, outcome: str, failure: Failure | None = None) -> None:
        super().__init__(outcome)
        self.status = status
        self.outcome = outcome
        self.failure = failure


class _Run:
    def __init__(
        self,
        capability: Capability,
        profile: Profile,
        inputs: dict[str, str],
        *,
        surface: Surface,
        guard: Guard,
        lease: Callable[[], LeaseToken],
        base_url: str,
        credentials: Credentials | None,
        evidence: EvidenceWriter | None,
        run_id: str,
        now_ms: Callable[[], int],
        sleep: Sleep,
        timing: Timing,
    ) -> None:
        self.cap, self.profile, self.inputs = capability, profile, inputs
        self.surface, self.guard, self.lease = surface, guard, lease
        self.base_url = base_url.rstrip("/")
        self.credentials, self.evidence, self.run_id = credentials, evidence, run_id
        self.now_ms, self.sleep, self.timing = now_ms, sleep, timing
        self.outputs: dict[str, Any] = {}
        self.drift: list[DriftWarning] = []
        self.recoveries: list[Recovery] = []
        self.timings: dict[str, int] = {}
        self.commit_state: CommitState = "none"
        self.url = self.base_url + "/"
        self.obs: Observation | None = None
        self.state: str | None = None
        self.step: Step | None = None
        self.started = now_ms()
        self._seq = 0

    # --- result assembly ---------------------------------------------------------------------------------------

    async def run(self) -> RunResult:
        try:
            self._check_inputs()
            await self._enter()
            for i, step in enumerate(self.cap.steps):
                await self._run_step(i, step)
            await self._finish()
            status, outcome, failure = "success", "success", None
        except _Stop as stop:
            status, outcome, failure = stop.status, stop.outcome, stop.failure
        except PolicyDenied as denied:
            self._emit("policy", policy=denied.decision)
            status, outcome = "failure", "POLICY_DENIED"
            failure = self._failure("POLICY_DENIED", "policy", f"{denied.decision.rule}: {denied.decision.reason}",
                                    approvable=denied.decision.approvable)
        except ResolutionError as e:
            status, outcome = "failure", e.code
            failure = self._failure(e.code, "locator", e.detail)
        except NativeDialogOpen:
            status, outcome = "failure", "NATIVE_DIALOG_OPEN"
            failure = self._failure("NATIVE_DIALOG_OPEN", "state",
                                    "a native dialog is blocking the page; it is never accepted automatically")
        except NotUniqueError:
            status, outcome = "failure", "TARGET_AMBIGUOUS"
            failure = self._failure("TARGET_AMBIGUOUS", "locator", "the surface found no single element")
        except SurfaceError as e:
            status, outcome = "failure", "SURFACE_ERROR"
            failure = self._failure("SURFACE_ERROR", "transient", f"the surface could not carry out the action "
                                                                    f"({type(e).__name__})")
        if self.commit_state == "unknown" and status == "failure":
            status, outcome = "indeterminate", "COMMIT_UNKNOWN"
            failure = self._failure("COMMIT_UNKNOWN", "commit", "the commit action's result is unknown; "
                                                                "check the application before retrying")
        self.timings["total_ms"] = self.now_ms() - self.started
        result = RunResult(
            run_id=self.run_id, capability=self.cap.id, version=self.cap.version,
            lock_hash=lock_hash(self.cap, self.profile), status=status, outcome=outcome,  # type: ignore[arg-type]
            outputs=self.outputs if status == "success" else {}, failure=failure, recoveries=self.recoveries,
            drift=self.drift,
            side_effects=SideEffects(commit_state=self.commit_state,
                                     by="automation" if self.commit_state != "none" else None),
            retry_safe=self.commit_state in ("none", "rejected"), timings=self.timings,
        )
        self._emit("result", detail={"status": status, "outcome": outcome})
        return result

    def _failure(self, code: str, category: str, detail: str, *, expected: str | None = None,
                 c: Classification | None = None, approvable: bool = False) -> Failure:
        observed = None
        if c is not None:
            observed = ObservedState(state=c.label, url=mask_url(self.url), near_misses=list(c.near_misses))
        return Failure(code=code, category=category,  # type: ignore[arg-type]
                       step_id=self.step.id if self.step else None,
                       step_intent=self.step.intent if self.step else None,
                       expected=expected, observed=observed, detail=detail, approvable=approvable)

    def _emit(self, kind: str, *, action: dict[str, Any] | None = None, policy: PolicyDecision | None = None,
              detail: dict[str, Any] | None = None) -> None:
        if self.evidence is None:
            return
        self._seq += 1
        self.evidence.event(EvidenceEvent(
            ts=datetime.now(UTC), seq=self._seq, run_id=self.run_id, actor="replay", kind=kind,
            step_id=self.step.id if self.step else None, state=self.state, action=action, policy=policy,
            lease=self.lease(), detail=detail or {}, duration_ms=None))

    # --- inputs ------------------------------------------------------------------------------------------------

    def _check_inputs(self) -> None:
        declared = self.cap.contract.inputs
        problems = [f"input {name!r} is missing" for name in declared if name not in self.inputs]
        problems += [f"input {name!r} is not declared" for name in self.inputs if name not in declared]
        for name, spec in declared.items():
            if name in self.inputs:
                try:
                    check_input(spec, self.inputs[name])
                except ValueRejected as e:
                    problems.append(f"input {name!r} is invalid: {e}")
        if problems:
            raise _Stop("failure", "INPUT_INVALID", self._failure("INPUT_INVALID", "caller", "; ".join(problems)))

    def _fill(self, text: str, *, encode: bool = False) -> str:
        return TEMPLATE_RE.sub(lambda m: quote(self.inputs[m.group(1)], safe="") if encode else self.inputs[m.group(1)],
                               text)

    # --- surface access ----------------------------------------------------------------------------------------

    def _request(self, kind: ActionKind, effect: str, *, actor: Actor = "replay",
                 target_url: str | None = None) -> ActionRequest:
        return ActionRequest(kind=kind, actor=actor, page_url=self.url, target_url=target_url,
                             effect=effect, lease=self.lease())  # type: ignore[arg-type]

    async def _observe(self) -> Observation:
        obs = await self.surface.observe(screenshot=False)
        if obs.facts.native_dialog is not None:
            raise NativeDialogOpen("a native dialog is open")
        self.obs, self.url = obs, obs.facts.url
        return obs

    async def _resolve(self, target_id: str, *, primary_only: bool = False) -> Resolved:
        target: Target = self.cap.targets[target_id]
        resolved = await resolve(self.surface, target, self.inputs, primary_only=primary_only)
        if (warning := resolved.drift(self.step.id if self.step else "-", target_id)) is not None:
            self.drift.append(warning)
            self._emit("drift", detail={"target": target_id, "used": warning.used_strategy})
        return resolved

    async def _element(self, target_id: str) -> object:
        return (await self._resolve(target_id)).element

    async def _read(self, element: object) -> str:
        return await self.surface.read_text(element)  # type: ignore[arg-type]

    async def _holds(self, pred: Predicate) -> bool:
        assert self.obs is not None
        page, _ = views_of(self.obs)
        return await evaluate(pred, view=page, inputs=self.inputs, resolve=self._element, read=self._read)

    # --- settling ----------------------------------------------------------------------------------------------

    async def _settle(self, accept: Callable[[Classification], _Verdict | None], *, timeout_ms: int, stable: int,
                      expected: str, fence: bool = False) -> _Verdict:
        """Classify until the accepted verdict is stable. `fence` (a navigating action just ran) first passes the
        surface's navigation fence: the load has finished and the main content and dialog layer read the same on two
        polls at least `poll_interval_ms` apart. Whether a screen that never settles is a failure is decided here,
        by the classifier: the fence only ever waits, then classification judges what is on the screen."""
        deadline = self.now_ms() + timeout_ms
        if fence:
            await self.surface.settle(interval_ms=self.timing.poll_interval_ms, timeout_ms=min(timeout_ms, 5_000))
        last: str | None = None
        streak = 0
        while True:
            obs = await self._observe()
            c = classify_observation(self.profile, obs)
            self.state = c.label
            verdict = accept(c)
            if verdict is not None:
                streak = streak + 1 if verdict.key == last else 1
                last = verdict.key
                if streak >= stable:
                    self._emit("state", detail={"state": c.label})
                    return verdict
            else:
                last, streak = None, 0
            if self.now_ms() >= deadline:
                code = {"unknown": "UNKNOWN_STATE", "ambiguous": "AMBIGUOUS_STATE"}.get(c.kind, "UNEXPECTED_STATE")
                detail = {
                    "UNKNOWN_STATE": "the screen matches no state in the app profile",
                    "AMBIGUOUS_STATE": f"the screen matches several states: {', '.join(c.candidates)}",
                    "UNEXPECTED_STATE": f"the screen is in state {c.label!r}, which this step does not allow",
                }[code]
                raise _Stop("failure", code, self._failure(code, "state", detail, expected=expected, c=c))
            await self.sleep(self.timing.poll_interval_ms / 1000)

    def _meaning(self, sid: str) -> _Verdict | None:
        meaning = self.profile.default_meaning.get(sid)
        if meaning is None:
            return None
        if meaning.failure is not None:
            return _Verdict("failure", f"failure:{meaning.failure}", code=meaning.failure, state=sid)
        if meaning.outcome in self.cap.contract.outcomes:
            return _Verdict("outcome", f"outcome:{meaning.outcome}", code=meaning.outcome, state=sid)
        return None  # a meaning the capability does not declare does not apply

    def _step_acceptor(self, step: Step, next_state: str) -> Callable[[Classification], _Verdict | None]:
        branches: dict[str, Branch] = {b.state: b for b in step.on}

        def accept(c: Classification) -> _Verdict | None:
            if c.kind != "state" or c.state is None:
                return None
            for sid in self.profile.lineage(c.state):  # most specific first
                branch = branches.get(sid)
                if branch is not None:
                    if branch.outcome is not None:
                        return _Verdict("outcome", f"outcome:{branch.outcome}", code=branch.outcome, state=sid)
                    return _Verdict("continue", "continue", state=sid)
                if sid == next_state:
                    return _Verdict("continue", "continue", state=sid)
                if (meaning := self._meaning(sid)) is not None:
                    return meaning
            return None

        return accept

    def _entry_acceptor(self, *, allow_login: bool) -> Callable[[Classification], _Verdict | None]:
        login = self.profile.login.state

        def accept(c: Classification) -> _Verdict | None:
            if c.kind != "state" or c.state is None:
                return None
            for sid in self.profile.lineage(c.state):
                if sid == self.profile.entry.start_state:
                    return _Verdict("continue", "continue", state=sid)
                if sid == login:
                    if allow_login:
                        return _Verdict("login", "login", state=sid)
                    return _Verdict("failure", "failure:LOGIN_FAILED", code="LOGIN_FAILED", state=sid)
                if (meaning := self._meaning(sid)) is not None:
                    return meaning
            return None

        return accept

    def _act_on_verdict(self, verdict: _Verdict, expected: str) -> None:
        if verdict.kind == "outcome":
            assert verdict.code is not None
            raise _Stop("business_outcome", verdict.code)
        if verdict.kind == "failure":
            assert verdict.code is not None
            detail = (f"the screen is in state {verdict.state!r}, which the profile treats as {verdict.code}")
            raise _Stop("failure", verdict.code, self._failure(verdict.code, "state", detail, expected=expected,
                                                                c=Classification("state", state=verdict.state)))

    # --- the run -----------------------------------------------------------------------------------------------

    async def _enter(self) -> None:
        start = self.profile.entry.start_state
        url = self.base_url + self.profile.entry.route
        decision = await self.surface.navigate(url, self._request("navigate", "none", target_url=url))
        self._emit("action", action={"kind": "navigate", "entry": True}, policy=decision)
        timeout = self.timing.entry_timeout_ms
        verdict = await self._settle(self._entry_acceptor(allow_login=True), timeout_ms=timeout,
                                     stable=self.timing.stable_polls, expected=start, fence=True)
        if verdict.kind == "login":
            await self._login()
            verdict = await self._settle(self._entry_acceptor(allow_login=False), timeout_ms=timeout,
                                         stable=self.timing.stable_polls, expected=start, fence=True)
        self._act_on_verdict(verdict, start)

    async def _login(self) -> None:
        if self.credentials is None:
            raise _Stop("failure", "LOGIN_REQUIRED", self._failure(
                "LOGIN_REQUIRED", "state", "the session is logged out and no credentials were supplied",
                expected=self.profile.entry.start_state))
        form, began = self.profile.login, self.now_ms()
        self._emit("recovery", detail={"handler": "login"})
        for target, text in ((form.username, self.credentials.username), (form.password, self.credentials.password)):
            resolved = await resolve(self.surface, target, {})
            await self.surface.type(resolved.element, text, self._request("type", "none", actor="handler"))
        resolved = await resolve(self.surface, form.submit, {})
        await self.surface.click(resolved.element, self._request("click", "none", actor="handler"))
        self.recoveries.append(Recovery(handler="login", state=form.state, step_id=None, at_ms=began - self.started))

    async def _run_step(self, i: int, step: Step) -> None:
        self.step = step
        began = self.now_ms()
        last = i + 1 == len(self.cap.steps)
        next_state = self.cap.success.state if last else self.cap.steps[i + 1].expect
        self._emit("action", action=step.do.model_dump(mode="json", exclude_none=True), detail={"effect": step.effect})
        await self._perform(step)
        stable = self.timing.stable_polls if step.navigates else 1
        verdict = await self._settle(self._step_acceptor(step, next_state), timeout_ms=step.timeout_ms, stable=stable,
                                     expected=next_state, fence=step.navigates)
        self._act_on_verdict(verdict, next_state)
        if step.verify is not None and not await self._holds(step.verify):
            raise _Stop("failure", "CHECKPOINT_FAILED", self._failure(
                "CHECKPOINT_FAILED", "state", "the value echo after the action does not match", expected=next_state))
        self.timings[step.id] = self.now_ms() - began

    async def _perform(self, step: Step) -> None:
        try:
            await self._do(step)
        except (PolicyDenied, ResolutionError, _Stop):
            raise  # nothing was done (or the run already ended with a verdict)
        except Exception:
            if step.effect == "commit":
                self.commit_state = "unknown"
            raise
        if step.effect == "commit":
            self.commit_state = "attempted"

    async def _do(self, step: Step) -> None:
        do = step.do
        decision: PolicyDecision | None = None
        if isinstance(do, NavigateAction):
            url = self.base_url + self._fill(do.route, encode=True)
            decision = await self.surface.navigate(url, self._request("navigate", step.effect, target_url=url))
        elif isinstance(do, ClickAction):
            element = await self._element(do.target)
            decision = await self.surface.click(element, self._request("click", step.effect))  # type: ignore[arg-type]
        elif isinstance(do, TypeAction):
            element = await self._element(do.target)
            decision = await self.surface.type(element, self._fill(do.text),  # type: ignore[arg-type]
                                               self._request("type", step.effect))
        elif isinstance(do, SelectAction):
            element = await self._element(do.target)
            decision = await self.surface.select(element, self._fill(do.option),  # type: ignore[arg-type]
                                                 self._request("select", step.effect))
        elif isinstance(do, PressAction):
            decision = await self.surface.press(do.key, self._request("press", step.effect))
        elif isinstance(do, ExtractAction):
            await self._extract(step, do)
        elif isinstance(do, AssertAction):
            if self.obs is None or not await self._holds(do.that):
                category = "data" if do.on_fail == "IDENTITY_MISMATCH" else "state"
                detail = ("the screen does not show the requested record" if do.on_fail == "IDENTITY_MISMATCH"
                          else "a required checkpoint does not hold")
                raise _Stop("failure", do.on_fail, self._failure(do.on_fail, category, detail))
        if decision is not None:
            self._emit("policy", policy=decision)

    async def _extract(self, step: Step, do: ExtractAction) -> None:
        resolved = await self._resolve(do.target, primary_only=True)
        decision = self.guard.check(self._request("extract", step.effect))
        self._emit("policy", policy=decision)
        if not decision.allowed:
            raise PolicyDenied(decision)
        shown = await self.surface.read_text(resolved.element)
        try:
            self.outputs[do.output] = parse_output(self.cap.contract.outputs[do.output], shown,
                                                   self.profile.number_format)
        except ValueRejected as e:
            raise _Stop("failure", "OUTPUT_UNPARSEABLE", self._failure(
                "OUTPUT_UNPARSEABLE", "data", f"output {do.output!r} could not be read from the screen: {e}")) from None

    async def _finish(self) -> None:
        self.step = None
        await self._observe()
        for check in self.cap.success.checks:
            if not await self._holds(check):
                raise _Stop("failure", "CHECKPOINT_FAILED", self._failure(
                    "CHECKPOINT_FAILED", "state", "the final screen does not show the required evidence",
                    expected=self.cap.success.state))
        if missing := [name for name in self.cap.success.outputs if name not in self.outputs]:
            raise _Stop("failure", "OUTPUT_MISSING", self._failure(
                "OUTPUT_MISSING", "data", f"outputs not produced: {', '.join(missing)}"))


async def replay(
    capability: Capability,
    profile: Profile,
    inputs: dict[str, str],
    *,
    surface: Surface,
    guard: Guard,
    lease: Callable[[], LeaseToken],
    base_url: str,
    credentials: Credentials | None = None,
    evidence: EvidenceWriter | None = None,
    run_id: str | None = None,
    timing: Timing | None = None,
    now_ms: Callable[[], int] | None = None,
    sleep: Sleep | None = None,
) -> RunResult:
    """Run `capability` once and return its `RunResult`. Never raises for application or policy outcomes."""
    return await _Run(
        capability, profile, dict(inputs), surface=surface, guard=guard, lease=lease, base_url=base_url,
        credentials=credentials, evidence=evidence, run_id=run_id or uuid.uuid4().hex[:12],
        now_ms=now_ms or (lambda: int(time.monotonic() * 1000)), sleep=sleep or asyncio.sleep,
        timing=timing or Timing(),
    ).run()
