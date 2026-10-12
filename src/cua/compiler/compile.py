"""Offline compiler v0: a recorded discovery trace becomes a capability draft.

    validate the run -> keep performed actions -> lift {{input}} tokens to ${input} -> ranked strategies per target
    -> attach states from the app profile -> identity asserts from token sightings -> static validation

Deterministic: the same trace and profile always give byte-identical JSON (no clock, no randomness, no dict-order
dependence beyond the trace's own order). The compiler reads only the persisted, redacted trace. It never touches a
live application or a model, and it never writes status or approval: a draft is a draft until the approval sidecar
binds to its lock hash.

Imports follow the contract: artifact, agent.actions, surface, safety.policy (via the trace types). Nothing here
imports a browser engine or a model SDK.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

from cua.agent.actions import AgentAction
from cua.artifact.classify import Classification, classify
from cua.artifact.lock import canonical, dumps, load_profile, sha256_hex
from cua.artifact.predicates import ScreenNode, ScreenView, TargetEqualsInput, TargetVisible
from cua.artifact.screen import view_from_outline
from cua.artifact.schema import (
    LOCATOR_FIELDS,
    Action,
    AssertAction,
    Capability,
    ClickAction,
    Contract,
    Effect,
    ExtractAction,
    NavigateAction,
    PressAction,
    Profile,
    Provenance,
    SelectAction,
    Step,
    Strategy,
    Success,
    Target,
    TypeAction,
    max_effect,
)
from cua.artifact.validate import ArtifactInvalid, require_valid
from cua.compiler.trace import RecordedAction, RecordedTrace, ScreenRecord, TargetRecord

COMPILER_ID = "cua.compiler/v0"

_TOKEN = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")
_TOKEN_ENCODED = re.compile(r"%7B%7B([a-z][a-z0-9_]*)%7D%7D", re.IGNORECASE)
_KEPT = ("navigate", "click", "type", "select", "press", "extract")
_REFUSED = {"request_approval": "approval-gated actions arrive with the write flow (M6)",
            "request_help": "a run that needed help is not a compilable read flow (M5)"}


class CompileError(ValueError):
    """The trace cannot become a capability. `problems` are safe to show: no input or output values."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def lift(text: str) -> str:
    """`{{name}}` (and its percent-encoded form inside URLs) -> `${name}`."""
    return _TOKEN.sub(r"${\1}", _TOKEN_ENCODED.sub(r"{{\1}}", text))


def _tokens_in(text: str) -> set[str]:
    return set(_TOKEN.findall(_TOKEN_ENCODED.sub(r"{{\1}}", text)))


def _intent(text: str) -> str:
    return " ".join(_TOKEN.sub(r"<\1>", text).split()) or "step"


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40].strip("_")
    return slug if slug and slug[0].isalpha() else f"t_{slug}".rstrip("_")


# --- screens -----------------------------------------------------------------------------------------------------


def _classify(profile: Profile, screen: ScreenRecord) -> Classification:
    page: ScreenView = view_from_outline(screen.url, screen.snapshot)
    dialog = ScreenView(url=screen.url, nodes=(ScreenNode(role="dialog", name=screen.dialog),)) if screen.dialog else None
    return classify(profile, page, dialog)


def _route(url: str, origin: tuple[str, str], problems: list[str], where: str) -> str:
    parts = urlsplit(lift(url))
    if (parts.scheme, parts.netloc) not in (origin, ("", "")):
        problems.append(f"{where}: navigates outside the application origin")
    path = parts.path or "/"
    return f"{path}?{parts.query}" if parts.query else path


# --- targets -----------------------------------------------------------------------------------------------------


class _Targets:
    def __init__(self, problems: list[str]) -> None:
        self.targets: dict[str, Target] = {}
        self._by_key: dict[str, str] = {}
        self._problems = problems

    def add(self, rec: TargetRecord, where: str) -> str:
        ranked = sorted(rec.candidates, key=lambda c: (c.rank, canonical(c.strategy)))
        strategies: list[Strategy] = []
        seen: set[bytes] = set()
        for cand in ranked:
            s = cand.strategy
            update = {f: lift(v) for f in LOCATOR_FIELDS if isinstance(v := getattr(s, f), str)}
            s = s.model_copy(update=update)
            if (key := canonical(s)) not in seen:
                seen.add(key)
                strategies.append(s)
        if not strategies:
            self._problems.append(f"{where}: no stable locator was recorded for the {rec.role} it acts on")
            return "unresolved"
        target = Target(description=strategies[0].describe(), strategies=strategies, fingerprint=rec.fingerprint)
        key = canonical(target).decode("utf-8")
        if key in self._by_key:
            return self._by_key[key]
        first = strategies[0]
        label = first.name or first.label or first.column or first.within_name or first.css or ""
        base = _slug(f"{rec.role}_{label}")
        tid, n = base, 2
        while tid in self.targets:
            tid, n = f"{base}_{n}", n + 1
        self.targets[tid] = target
        self._by_key[key] = tid
        return tid


# --- the compiler ------------------------------------------------------------------------------------------------


def _argument_text(action: AgentAction) -> list[str]:
    return [v for f in ("text", "option", "url", "name", "key") if isinstance(v := getattr(action, f, None), str)]


def _validate_run(trace: RecordedTrace, profile: Profile) -> list[str]:
    problems: list[str] = []
    if trace.app != profile.app:
        problems.append(f"trace is for app {trace.app!r} but the profile is for {profile.app!r}")
    if trace.outcome != "done":
        problems.append("the run did not finish: only a completed run is compiled")
    if not trace.actions:
        problems.append("the trace has no actions")
        return problems
    seqs = [a.seq for a in trace.actions]
    if seqs != sorted(set(seqs)):
        problems.append("action sequence numbers must be unique and increasing")
    last = trace.actions[-1]
    if last.action.kind != "done" or last.status != "performed":
        problems.append("the run must end with a performed done action")
    extracted: dict[str, int] = {}
    for a in trace.actions:
        where = f"action {a.seq}"
        if a.action.kind in _REFUSED:
            problems.append(f"{where}: {_REFUSED[a.action.kind]}")
        used = set(a.tokens)
        for text in _argument_text(a.action):
            used |= _tokens_in(text)
        if unknown := used - set(trace.inputs):
            problems.append(f"{where}: uses undeclared input token(s) {sorted(unknown)}")
        if a.status == "performed" and a.action.kind == "extract":
            if a.action.name not in trace.outputs:
                problems.append(f"{where}: extracts undeclared output {a.action.name!r}")
            extracted[a.action.name] = extracted.get(a.action.name, 0) + 1
    for name in trace.outputs:
        count = extracted.get(name, 0)
        if count == 0:
            problems.append(f"output {name!r} was never extracted")
        elif count > 1:
            problems.append(f"output {name!r} was extracted {count} times; one extract per output")
    return problems


def compile_trace(
    trace: RecordedTrace,
    profile: Profile,
    *,
    name: str,
    discovered_by: str,
    title: str | None = None,
    description: str | None = None,
    version: str = "1.0.0",
    echo_verify: bool = True,
) -> Capability:
    """Compile a completed read-flow trace into a capability draft, or raise `CompileError`."""
    problems = _validate_run(trace, profile)
    if problems:
        raise CompileError(problems)

    entry = urlsplit(trace.entry_url)
    origin = (entry.scheme, entry.netloc)
    targets = _Targets(problems)
    steps: list[Step] = []
    seen_pairs: set[tuple[str, str]] = set()  # (input, target) pairs whose value is already checked

    def state_of(screen: ScreenRecord, where: str) -> str:
        c = _classify(profile, screen)
        if c.kind != "state" or c.state is None:
            hint = f" (closest: {', '.join(c.near_misses)})" if c.near_misses else ""
            problems.append(f"{where}: the screen is {c.kind} in the app profile{hint}")
            return "unresolved"
        return c.state

    def target_of(a: RecordedAction, where: str) -> str:
        if a.target is None:
            problems.append(f"{where}: no target was recorded for the element it acts on")
            return "unresolved"
        return targets.add(a.target, where)

    kept = [a for a in trace.actions if a.status == "performed" and a.action.kind in _KEPT]
    for a in kept:
        where = f"action {a.seq}"
        act = a.action
        verify = None
        do: Action
        if act.kind == "navigate":
            do = NavigateAction(route=_route(act.url, origin, problems, where))
        elif act.kind == "click":
            do = ClickAction(target=target_of(a, where))
        elif act.kind == "type":
            tid = target_of(a, where)
            do = TypeAction(target=tid, text=lift(act.text))
            if echo_verify and (m := re.fullmatch(r"\$\{([a-z][a-z0-9_]*)\}", do.text)):
                verify = TargetEqualsInput(target=tid, input=m.group(1), normalizer="exact")
                seen_pairs.add((m.group(1), tid))
        elif act.kind == "select":
            do = SelectAction(target=target_of(a, where), option=lift(act.option))
        elif act.kind == "press":
            if act.key not in ("Enter", "Tab", "Escape"):
                problems.append(f"{where}: key {act.key!r} is outside the closed key set")
            do = PressAction(key=act.key if act.key in ("Enter", "Tab", "Escape") else "Enter")
        else:  # extract
            do = ExtractAction(target=target_of(a, where), output=act.name)
        navigates = act.kind == "navigate" or (a.post is not None and a.post.url != a.pre.url)
        steps.append(Step(id="s00", intent=_intent(act.rationale), expect=state_of(a.pre, where), do=do,
                          effect=a.effect, navigates=navigates, verify=verify))
        for sighting in a.sightings:
            tid = targets.add(sighting.target, where)
            pair = (sighting.token, tid)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            if a.post is None:
                problems.append(f"{where}: a value was sighted but no screen was recorded after the action")
                continue
            steps.append(Step(
                id="s00", intent=f"The screen shows the requested <{sighting.token}>", expect=state_of(a.post, where),
                do=AssertAction(that=TargetEqualsInput(target=tid, input=sighting.token, normalizer="text"),
                                on_fail="IDENTITY_MISMATCH"),
                effect="none"))

    done = trace.actions[-1]
    success_state = state_of(done.pre, "done")
    checks = []
    for rec in trace.done_evidence:
        pred = TargetVisible(target=targets.add(rec, "done"))
        if pred not in checks:
            checks.append(pred)

    if problems:
        raise CompileError(problems)

    # ids are positional so a re-recorded trace with the same shape yields the same ids
    steps = [s.model_copy(update={"id": f"s{i:02d}_{s.do.kind}"}) for i, s in enumerate(steps, start=1)]
    effect: Effect = max_effect([s.effect for s in steps])
    cap_id = f"{trace.app}.{name}"
    capability = Capability(
        id=cap_id,
        version=version,
        title=title or name.replace("_", " ").capitalize(),
        description=description or _intent(trace.goal),
        app=trace.app,
        contract=Contract(inputs=trace.inputs, outputs=trace.outputs, outcomes={}),
        effect=effect,
        steps=steps,
        success=Success(state=success_state, outputs=list(trace.outputs), checks=checks),
        targets=targets.targets,
        provenance=Provenance(trace_run_id=trace.run_id, trace_sha256=sha256_hex(canonical(trace)),
                              discovered_by=discovered_by, compiler=COMPILER_ID),
    )
    try:
        require_valid(capability, profile)
    except ArtifactInvalid as e:
        raise CompileError(e.problems) from None
    return capability


def compile_to_json(trace: RecordedTrace, profile: Profile, **kwargs: str | bool | None) -> str:
    """The draft as file text (what `cua compile` writes)."""
    return dumps(compile_trace(trace, profile, **kwargs))  # type: ignore[arg-type]


def compile_file(trace_path: Path, profile_path: Path, **kwargs: str | bool | None) -> str:
    trace = RecordedTrace.model_validate_json(Path(trace_path).read_bytes())
    return compile_to_json(trace, load_profile(Path(profile_path)), **kwargs)

