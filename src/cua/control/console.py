"""The operator console: a small local FastAPI app for the humans who take over a run, plus the hub the run waits on.

Owner: Agent C. Two halves that share one `InterventionHub`:

* `InterventionHub` is what the interpreter calls. `await hub.escalate(request)` hands control to a person: it moves the
  lease (`automation -> none`), writes the intervention request into the run bundle, emits the interim `escalated`
  line (`notify`), marks the automation drained at a step boundary, and then BLOCKS until an operator resumes, aborts,
  marks the run complete, approves a pending action or gives a verdict. It returns an `EscalationOutcome`. The SLA
  applies only while the request is unclaimed (holder `none`); a claimed lease never expires.
* `build_app(hub)` / `ConsoleServer(hub)` are the pages and JSON endpoints an operator uses. Every action goes through
  `LeaseManager` (claim, release, abort) and the resume rules (`cua.control.resume`), and is logged as an evidence
  event `kind="intervention"`, actor `human`, with the operator id (refusals included).

One asyncio loop hosts all of it, as spike S2 proved: `ConsoleServer.serve()` is an awaitable that runs next to the run
and Playwright (`async with server.running(): ...`). A request is "claimable" only after the lease drained to a step
boundary, so a person never takes a page the automation is still acting on.

Actions (POST `/api/interventions/<id>/<action>`, JSON body with the `operator` id):

    claim          none -> human:<operator>; refused while the automation has not drained
    resume         restart from the entry on the same session; refused by `decide_resume`; at most once per handoff
    abort          ends the run, from any state
    approve        release to automation WITH a single-use approval bound to (action hash, the new lease generation)
    mark-complete  only if the success checkpoint holds and the outputs can be extracted (callbacks the interpreter gave)
    verdict        `resolve_commit` for a COMMIT_UNKNOWN intervention: committed | not_committed | unknown, recorded as an
                   event and `verdicts/<id>.json`; a closed bundle can be reviewed and given a verdict afterwards
                   (`InterventionHub.review`)

SECURITY LIMITS (REPORT cites this): this is a local demo console. It binds to loopback only (`ConsoleServer` refuses any
other host) and has NO authentication: the operator id is a self-declared label, not an identity. It is protected from
other web pages only by a Host-header allow-list (DNS rebinding), an Origin check on POSTs and JSON-only bodies
(cross-site form posts); anyone with a local account can still act as an operator. Pages are server-rendered with every
value escaped and carry no values from the application (URLs have no query; screenshots are the surface's masked ones).
Do not expose it beyond the machine.

Approvals (`ApprovalStore`) are in memory: `issue` returns an opaque id bound to (action hash, lease generation),
`consume` accepts it once, for exactly that action at exactly that generation. Any consume attempt on a known id spends
it, so a mismatched or replayed attempt cannot be retried into success.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import html
import json
import re
import secrets
import socket
import sys
import threading
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TextIO

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from cua.artifact.schema import Profile
from cua.control.capture import CaptureEvent, RequestLogEntry, commit_hit_sink, evidence_sink
from cua.control.interventions import COMMIT_UNKNOWN, InterventionRequest, write_intervention
from cua.control.lease import AUTOMATION, NOBODY, LeaseError, LeaseManager, LeaseToken, human
from cua.control.resume import (
    CheckpointEvaluator,
    HandoffRecord,
    OutputExtractor,
    decide_mark_complete,
)
from cua.evidence.events import EvidenceWriter

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
ALLOWED_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})
VERDICTS = ("committed", "not_committed", "unknown", "unsure")
VERDICT_SCHEMA = "cua.verdict/v1"
_OPERATOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")

Verdict = Literal["committed", "not_committed", "unknown", "unsure"]
OutcomeKind = Literal["resume", "abort", "mark_complete", "approved", "verdict"]
Status = Literal["draining", "open", "claimed", "resolved", "review"]


class ConsoleError(Exception):
    """An operator action that was refused. Nothing changed. `status` is the HTTP status it maps to."""

    def __init__(self, status: int, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.status = status
        self.code = code
        self.message = message or code


# ------------------------------------------------------------------------------------------------------ approvals


def action_hash(descriptor: Mapping[str, Any]) -> str:
    """A stable hash of a pending action's descriptor (kind, target, effect, never typed values): sha256 of its
    canonical JSON. The console binds an approval to this and to the lease generation."""
    canonical = json.dumps(descriptor, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def fingerprint(approval_id: str) -> str:
    """What evidence says about an approval: never the id itself."""
    return hashlib.sha256(approval_id.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class ApprovalGrant:
    approval_id: str  # opaque and secret: it travels to the action as `ActionRequest.approval_id`
    action_hash: str
    generation: int
    operator: str
    issued_at: datetime


ApprovalReason = Literal["ok", "unknown", "already_used", "action_mismatch", "generation_mismatch"]


@dataclass(frozen=True)
class ApprovalCheck:
    ok: bool
    reason: ApprovalReason
    grant: ApprovalGrant | None = None


class ApprovalStore:
    """Single-use approvals bound to (action hash, lease generation). Thread-safe, in memory."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._live: dict[str, ApprovalGrant] = {}
        self._spent: set[str] = set()

    def issue(self, *, action_hash: str, generation: int, operator: str) -> ApprovalGrant:
        grant = ApprovalGrant(
            approval_id="appr-" + secrets.token_urlsafe(18), action_hash=action_hash, generation=generation,
            operator=operator, issued_at=self._clock(),
        )
        with self._lock:
            self._live[grant.approval_id] = grant
        return grant

    def consume(self, approval_id: str | None, *, action_hash: str, generation: int) -> ApprovalCheck:
        """Accept the approval for this action at this generation, once. A known id is spent by ANY attempt."""
        with self._lock:
            if approval_id is None:
                return ApprovalCheck(False, "unknown")
            grant = self._live.pop(approval_id, None)
            if grant is None:
                return ApprovalCheck(False, "already_used" if approval_id in self._spent else "unknown")
            self._spent.add(approval_id)
        if grant.generation != generation:
            return ApprovalCheck(False, "generation_mismatch", grant)
        if not secrets.compare_digest(grant.action_hash, action_hash):
            return ApprovalCheck(False, "action_mismatch", grant)
        return ApprovalCheck(True, "ok", grant)

    @property
    def outstanding(self) -> int:
        with self._lock:
            return len(self._live)


# ------------------------------------------------------------------------------------------------------- outcomes


@dataclass(frozen=True)
class EscalationOutcome:
    """How an escalation ended, and what the interpreter does next.

    resume         restart from the entry on the same session; `lease` is the automation's NEW token (take a fresh one)
    abort          stop the run; `reason` says why (`operator_abort`, `sla_timeout`, `run_cancelled`)
    mark_complete  finish as success with `outputs` (the success checkpoint held and the outputs were extracted)
    approved       continue in place; present `approval_id` on the pending commit's `ActionRequest` (bound to
                   `action_hash` and `lease.generation`; redeem it with `ApprovalStore.consume`)
    verdict        the operator's `resolve_commit` verdict is in `verdict`; the run ends with it
    """

    kind: OutcomeKind
    intervention_id: str
    operator: str | None
    reason: str
    lease: LeaseToken
    outputs: dict[str, Any] | None = field(default=None, repr=False)  # value-bearing: never logged
    approval_id: str | None = field(default=None, repr=False)  # a credential: never logged
    action_hash: str | None = None
    verdict: Verdict | None = None


class Escalation:
    """The handle `InterventionHub.register` returns: the registered request and, once it ends, its outcome."""

    def __init__(
        self,
        request: InterventionRequest,
        handoff: HandoffRecord,
        *,
        action_hash: str | None,
        checkpoint: CheckpointEvaluator | None,
        extract_outputs: OutputExtractor | None,
    ) -> None:
        self.request = request
        self.handoff = handoff
        self.action_hash = action_hash
        self.checkpoint = checkpoint
        self.extract_outputs = extract_outputs
        self.outcome: EscalationOutcome | None = None
        self.claimed_by: str | None = None
        self.verdict: Verdict | None = None
        self.review = False
        self.busy = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._done: asyncio.Future[EscalationOutcome] | None = None

    @property
    def id(self) -> str:
        return self.request.id

    def _future(self) -> asyncio.Future[EscalationOutcome]:
        if self._done is None:
            self._loop = asyncio.get_running_loop()
            self._done = self._loop.create_future()
            if self.outcome is not None:
                self._done.set_result(self.outcome)
        return self._done

    def _finish(self, outcome: EscalationOutcome) -> None:
        self.outcome = outcome
        done, loop = self._done, self._loop
        if done is None or loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            if not done.done():
                done.set_result(outcome)
        elif not loop.is_closed():  # an endpoint served on another loop (tests): wake the run's loop safely
            loop.call_soon_threadsafe(lambda: done.done() or done.set_result(outcome))

    async def wait(self) -> EscalationOutcome:
        return await self._future()


def jsonl_notifier(stream: TextIO | None = None) -> Callable[[dict[str, Any]], None]:
    """A `notify` that prints each interim event as one JSON line (default stderr, so stdout stays the result)."""

    def notify(line: dict[str, Any]) -> None:
        print(json.dumps(line, ensure_ascii=False), file=stream or sys.stderr, flush=True)

    return notify


# ---------------------------------------------------------------------------------------------------------- the hub


class InterventionHub:
    """The run's escalation state: the lease, the open interventions, the approvals and the evidence writer.

    `lease=None` makes a review hub (a closed bundle, see `review`): the queue and contexts are readable, no run is
    waiting, and only `verdict` can be recorded. `profile` classifies the human's captured actions for the resume
    rules. `notify` receives the interim `escalated` line (a plain dict). `poll_seconds` is how often a waiting
    `escalate` checks the SLA and an abort that came from outside the console.
    """

    def __init__(
        self,
        *,
        run_id: str,
        writer: EvidenceWriter | None,
        lease: LeaseManager | None,
        profile: Profile | None = None,
        approvals: ApprovalStore | None = None,
        notify: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        poll_seconds: float = 0.25,
        bundle_dir: Path | None = None,
    ) -> None:
        self.run_id = run_id
        self.writer = writer
        self.lease = lease
        self.profile = profile
        self.approvals = approvals or ApprovalStore(clock=clock)
        self.notify = notify
        self.console_url: str | None = None
        self._clock = clock or (lambda: datetime.now(UTC))
        self._poll = poll_seconds
        self._bundle_dir = bundle_dir or (writer.run_dir if writer is not None else None)
        self._items: dict[str, Escalation] = {}

    # ------------------------------------------------------------------------------------------- construction

    @classmethod
    def review(cls, run_dir: Path, *, clock: Callable[[], datetime] | None = None) -> InterventionHub:
        """A hub over a closed run bundle's `interventions/*.json`, for review after the fact."""
        run_dir = Path(run_dir).resolve()
        folder = run_dir / "interventions"
        if not folder.is_dir():
            raise FileNotFoundError(f"{run_dir.name} has no interventions/ folder")
        hub = cls(run_id=run_dir.name, writer=None, lease=None, clock=clock, bundle_dir=run_dir)
        for path in sorted(folder.glob("*.json")):
            request = InterventionRequest.model_validate_json(path.read_text(encoding="utf-8"))
            item = Escalation(
                request,
                HandoffRecord(request.id, high_water_effect=request.effect_so_far,
                              commit_attempted=request.commit_attempted),
                action_hash=None, checkpoint=None, extract_outputs=None,
            )
            item.review = True
            verdict_file = run_dir / "verdicts" / f"{request.id}.json"
            if verdict_file.is_file():
                item.verdict = json.loads(verdict_file.read_text(encoding="utf-8")).get("verdict")
            hub._items[request.id] = item
        return hub

    @property
    def live(self) -> bool:
        return self.lease is not None

    # ---------------------------------------------------------------------------------------------- escalating

    def register(
        self,
        request: InterventionRequest,
        *,
        action_hash: str | None = None,
        checkpoint: CheckpointEvaluator | None = None,
        extract_outputs: OutputExtractor | None = None,
    ) -> Escalation:
        """Hand control to a human without waiting: move the lease to `none` (unless it already is), stamp the
        request with that lease, write it to the bundle, emit the `escalated` line. The request shows as claimable
        only once the automation has drained to a step boundary (`escalate` does that for you).

        `action_hash` makes the escalation approvable (a pending commit). `checkpoint` and `extract_outputs` make it
        completable (see `cua.control.resume.decide_mark_complete`)."""
        if self.lease is None or self.writer is None:
            raise ConsoleError(409, "run_not_live", "this hub reviews a closed bundle")
        if request.id in self._items:
            raise ValueError(f"intervention {request.id!r} is already registered")
        if request.run_id != self.run_id:
            raise ValueError("the intervention belongs to another run")
        if self.lease.aborted:
            raise LeaseError("the run was aborted")
        token = self.lease.request_human(request.reason) if self.lease.holder == AUTOMATION else self.lease.token()
        request = request.model_copy(update={"lease": token})  # the lease the escalation happened under
        item = Escalation(
            request,
            HandoffRecord(request.id, high_water_effect=request.effect_so_far, commit_attempted=request.commit_attempted,
                          profile=self.profile),
            action_hash=action_hash, checkpoint=checkpoint, extract_outputs=extract_outputs,
        )
        write_intervention(self.writer, request)
        self._items[request.id] = item
        self._notify(item)
        return item

    async def escalate(
        self,
        request: InterventionRequest,
        *,
        action_hash: str | None = None,
        checkpoint: CheckpointEvaluator | None = None,
        extract_outputs: OutputExtractor | None = None,
    ) -> EscalationOutcome:
        """Escalate and block until the operator ends it. Call this where the automation is at a step boundary.

        Registers the request (see `register`), then holds the automation at the boundary (`step_boundary`, which
        marks the lease drained and makes the request claimable) until the operator resumes, aborts, completes,
        approves or gives a verdict, or the SLA expires an unclaimed request. Cancelling the caller aborts the lease
        with reason `run_cancelled`."""
        assert self.lease is not None
        item = self.register(request, action_hash=action_hash, checkpoint=checkpoint, extract_outputs=extract_outputs)
        boundary = asyncio.create_task(self.lease.step_boundary())
        try:
            return await self._wait(item)
        except asyncio.CancelledError:
            self._abandon(item)
            raise
        finally:
            boundary.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await boundary

    async def _wait(self, item: Escalation) -> EscalationOutcome:
        done = item._future()
        while not done.done():
            await asyncio.wait({done}, timeout=self._poll)
            if not done.done():
                self._tick(item)
        return done.result()

    def _tick(self, item: Escalation) -> None:
        """Between polls: an abort that did not come from the console, then the SLA (unclaimed requests only)."""
        lease = self.lease
        assert lease is not None
        if item.outcome is not None:
            return
        if lease.aborted:
            self._resolve(item, "abort", reason=lease.lease.reason, operator=None)
        elif lease.holder == NOBODY and item.request.overdue(self._clock()):
            with contextlib.suppress(LeaseError):
                lease.abort(None, "sla_timeout")
            self._resolve(item, "abort", reason="sla_timeout", operator=None)

    def _abandon(self, item: Escalation) -> None:
        assert self.lease is not None
        if not self.lease.aborted:
            with contextlib.suppress(LeaseError):
                self.lease.abort(None, "run_cancelled")
        if item.outcome is None:
            self._resolve(item, "abort", reason="run_cancelled", operator=None)

    def tick(self) -> None:
        """Run the SLA and external-abort checks now (what `escalate` does every `poll_seconds`)."""
        for item in list(self._items.values()):
            if self.lease is not None and item.outcome is None:
                self._tick(item)

    # ------------------------------------------------------------------------------------------------ reading

    def _get(self, intervention_id: str) -> Escalation:
        item = self._items.get(intervention_id)
        if item is None:
            raise ConsoleError(404, "unknown_intervention")
        return item

    def status(self, item: Escalation) -> Status:
        if item.review:
            return "review"
        if item.outcome is not None:
            return "resolved"
        assert self.lease is not None
        if self.lease.holder.startswith("human:"):
            return "claimed"
        return "open" if self.lease.is_drained else "draining"

    def queue(self) -> list[dict[str, Any]]:
        """Every intervention, open ones first, as short summaries."""
        rows = [self._summary(item) for item in self._items.values()]
        rows.sort(key=lambda r: (r["status"] == "resolved" or r["status"] == "review", r["created_at"]))
        return rows

    def open_interventions(self) -> list[Escalation]:
        return [i for i in self._items.values() if not i.review and i.outcome is None]

    def _summary(self, item: Escalation) -> dict[str, Any]:
        r = item.request
        return {
            "id": r.id, "status": self.status(item), "reason": r.reason, "capability": r.capability,
            "step_id": r.step_id, "created_at": r.created_at.isoformat(), "url": r.url,
            "sla_deadline": r.sla_deadline.isoformat() if r.sla_deadline else None,
        }

    def context(self, intervention_id: str) -> dict[str, Any]:
        """One intervention as the operator sees it. No typed values, no query strings."""
        item = self._get(intervention_id)
        r = item.request
        decision = item.handoff.decide()
        current = self.lease.lease if self.lease is not None else None
        h = item.handoff
        return {
            **self._summary(item),
            "run_id": r.run_id, "version": r.version, "detail": r.detail,
            "classified_state": r.classified_state, "expected_state": r.expected_state,
            "screenshot": r.screenshot is not None and self._screenshot_path(item) is not None,
            "effect_so_far": r.effect_so_far, "commit_attempted": r.commit_attempted,
            "escalated_lease": r.lease.model_dump(),
            "lease": None if current is None else {
                "holder": current.holder, "generation": current.generation, "reason": current.reason,
                "since": current.since.isoformat(),
            },
            "sla_applies": current is not None and current.holder == NOBODY and item.outcome is None,
            "resume": {"allowed": decision.allowed, "reason": decision.reason, "message": decision.message,
                       "resumes_used": h.resumes},
            "human_commit_signals": {"clicks": h.human_commit_clicks, "request_log_hits": h.request_log_commit_hits},
            "can_approve": item.action_hash is not None,
            "action_hash": item.action_hash[:12] if item.action_hash else None,
            "can_mark_complete": item.checkpoint is not None and item.extract_outputs is not None,
            "accepts_verdict": r.reason == COMMIT_UNKNOWN,
            "verdict": item.verdict,
            "outcome": item.outcome.kind if item.outcome else None,
        }

    def _screenshot_path(self, item: Escalation) -> Path | None:
        rel = item.request.screenshot
        if rel is None or self._bundle_dir is None:
            return None
        root = self._bundle_dir.resolve()
        path = (root / rel).resolve()
        if path.suffix.lower() != ".png" or not path.is_relative_to(root) or not path.is_file():
            return None
        return path

    def screenshot(self, intervention_id: str) -> Path:
        path = self._screenshot_path(self._get(intervention_id))
        if path is None:
            raise ConsoleError(404, "no_screenshot")
        return path

    # ----------------------------------------------------------------------------------------------- actions

    @staticmethod
    def _operator(operator: str) -> str:
        if not isinstance(operator, str) or not _OPERATOR.fullmatch(operator):
            raise ConsoleError(400, "bad_operator", "an operator id is 1-64 characters of letters, digits, '.', '_' or '-'")
        return operator

    def _live_item(self, intervention_id: str) -> Escalation:
        item = self._get(intervention_id)
        if item.review or self.lease is None:
            raise ConsoleError(409, "run_not_live", "this bundle is open for review only")
        if item.outcome is not None:
            raise ConsoleError(409, "already_resolved", f"the intervention ended with {item.outcome.kind}")
        if self.lease.aborted:
            raise ConsoleError(409, "run_aborted")
        return item

    def _held_by(self, item: Escalation, operator: str) -> None:
        assert self.lease is not None
        if self.lease.holder != human(operator):
            raise ConsoleError(409, "not_holder", "the lease is not held by this operator; claim it first")

    def _log(self, action: str, item: Escalation, operator: str | None, *, ok: bool, reason: str | None = None,
             **extra: Any) -> None:
        if self.writer is None:
            return
        self.writer.emit(
            "intervention", actor="human", step_id=item.request.step_id,
            lease=self.lease.token() if self.lease is not None else item.request.lease,
            detail={"action": action, "operator": operator, "intervention": item.id, "ok": ok, "reason": reason, **extra},
        )

    def _refuse(self, action: str, item: Escalation, operator: str, error: ConsoleError) -> ConsoleError:
        self._log(action, item, operator, ok=False, reason=error.code)
        return error

    def _resolve(self, item: Escalation, kind: OutcomeKind, *, reason: str, operator: str | None, **fields: Any) -> EscalationOutcome:
        assert self.lease is not None
        outcome = EscalationOutcome(
            kind=kind, intervention_id=item.id, operator=operator, reason=reason, lease=self.lease.token(), **fields
        )
        item._finish(outcome)
        return outcome

    async def claim(self, intervention_id: str, operator: str) -> LeaseToken:
        operator = self._operator(operator)
        item = self._live_item(intervention_id)
        assert self.lease is not None
        try:
            if self.status(item) == "draining":
                raise ConsoleError(409, "not_drained", "the automation has not reached a step boundary yet")
            token = self.lease.claim(operator)
        except LeaseError as e:
            raise self._refuse("claim", item, operator, ConsoleError(409, "not_claimable", str(e))) from e
        except ConsoleError as e:
            raise self._refuse("claim", item, operator, e) from e
        item.claimed_by = operator
        self._log("claim", item, operator, ok=True, generation=token.generation)
        return token

    async def resume(self, intervention_id: str, operator: str) -> EscalationOutcome:
        operator = self._operator(operator)
        item = self._live_item(intervention_id)
        assert self.lease is not None
        try:
            self._held_by(item, operator)
            decision = item.handoff.consume_resume()
            if not decision.allowed:
                raise ConsoleError(409, decision.reason, decision.message)
            self.lease.release_to_automation(operator, "operator_resume")
        except LeaseError as e:
            raise self._refuse("resume", item, operator, ConsoleError(409, "lease", str(e))) from e
        except ConsoleError as e:
            raise self._refuse("resume", item, operator, e) from e
        self._log("resume", item, operator, ok=True, reason="ok", generation=self.lease.generation)
        return self._resolve(item, "resume", reason="operator_resume", operator=operator)

    async def abort(self, intervention_id: str, operator: str) -> EscalationOutcome:
        operator = self._operator(operator)
        item = self._live_item(intervention_id)
        assert self.lease is not None
        try:
            self.lease.abort(operator, "operator_abort")
        except LeaseError as e:
            raise self._refuse("abort", item, operator, ConsoleError(409, "lease", str(e))) from e
        self._log("abort", item, operator, ok=True, generation=self.lease.generation)
        return self._resolve(item, "abort", reason="operator_abort", operator=operator)

    async def approve(self, intervention_id: str, operator: str, *, expected_action_hash: str | None = None) -> EscalationOutcome:
        """Release to the automation with a single-use approval for the pending action, bound to its hash and to
        the generation the automation will hold. `expected_action_hash`, if given, must equal the pending one."""
        operator = self._operator(operator)
        item = self._live_item(intervention_id)
        assert self.lease is not None
        try:
            self._held_by(item, operator)
            if item.action_hash is None:
                raise ConsoleError(409, "nothing_to_approve", "this escalation has no pending action")
            if expected_action_hash is not None and not secrets.compare_digest(expected_action_hash, item.action_hash):
                raise ConsoleError(409, "action_mismatch", "the approved action is not the pending action")
            token = self.lease.release_to_automation(operator, "operator_approve")
        except LeaseError as e:
            raise self._refuse("approve", item, operator, ConsoleError(409, "lease", str(e))) from e
        except ConsoleError as e:
            raise self._refuse("approve", item, operator, e) from e
        grant = self.approvals.issue(action_hash=item.action_hash, generation=token.generation, operator=operator)
        self._log("approve", item, operator, ok=True, generation=token.generation, approval=fingerprint(grant.approval_id),
                  action_hash=item.action_hash[:12])
        return self._resolve(item, "approved", reason="operator_approve", operator=operator,
                             approval_id=grant.approval_id, action_hash=item.action_hash)

    async def mark_complete(self, intervention_id: str, operator: str) -> EscalationOutcome:
        operator = self._operator(operator)
        item = self._live_item(intervention_id)
        assert self.lease is not None
        try:
            self._held_by(item, operator)
            if item.busy:
                raise ConsoleError(409, "busy", "a completion check is already running")
        except ConsoleError as e:
            raise self._refuse("mark_complete", item, operator, e) from e
        item.busy = True
        try:
            decision = await decide_mark_complete(checkpoint=item.checkpoint, extract_outputs=item.extract_outputs)
        finally:
            item.busy = False
        if item.outcome is not None or self.lease.holder != human(operator):
            raise self._refuse("mark_complete", item, operator, ConsoleError(409, "changed", "the lease changed meanwhile"))
        if not decision.allowed:
            raise self._refuse("mark_complete", item, operator, ConsoleError(409, decision.reason, decision.message))
        self._log("mark_complete", item, operator, ok=True, reason="ok", outputs=sorted(decision.outputs or {}))
        return self._resolve(item, "mark_complete", reason="operator_mark_complete", operator=operator,
                             outputs=decision.outputs or {})

    async def verdict(self, intervention_id: str, operator: str, verdict: str, note: str = "") -> dict[str, Any]:
        """Record the operator's `resolve_commit` verdict for a COMMIT_UNKNOWN intervention: an event and
        `verdicts/<id>.json` in the bundle. In a live run the claiming operator ends the escalation with it (the run
        finishes `indeterminate`, resolved); a reviewed bundle only gains the record. Once per intervention."""
        operator = self._operator(operator)
        item = self._get(intervention_id)
        try:
            if verdict not in VERDICTS:
                raise ConsoleError(400, "bad_verdict", f"verdict must be one of {', '.join(VERDICTS)}")
            if item.request.reason != COMMIT_UNKNOWN:
                raise ConsoleError(409, "not_commit_unknown", "a verdict resolves a COMMIT_UNKNOWN intervention")
            if item.verdict is not None:
                raise ConsoleError(409, "verdict_recorded", "a verdict was already recorded")
            if not item.review:
                item = self._live_item(intervention_id)
                self._held_by(item, operator)
        except ConsoleError as e:
            if not item.review:
                raise self._refuse("resolve_commit", item, operator, e) from e
            raise
        record = {
            "schema": VERDICT_SCHEMA, "action": "resolve_commit", "intervention_id": item.id, "run_id": item.request.run_id,
            "capability": item.request.capability, "step_id": item.request.step_id, "verdict": verdict,
            "operator": operator, "note": note[:500], "at": self._clock().isoformat(), "mode": "review" if item.review else "live",
            "escalated_lease": item.request.lease.model_dump(),
        }
        self._write_verdict(item, operator, verdict, record)
        item.verdict = verdict  # type: ignore[assignment]
        if not item.review:
            self._resolve(item, "verdict", reason="operator_verdict", operator=operator, verdict=verdict)  # type: ignore[arg-type]
        return record

    def _write_verdict(self, item: Escalation, operator: str, verdict: str, record: dict[str, Any]) -> None:
        if self.writer is not None:
            self.writer.json(f"verdicts/{item.id}.json", record)
            self._log("resolve_commit", item, operator, ok=True, verdict=verdict, record=f"verdicts/{item.id}.json")
            return
        if self._bundle_dir is None:
            raise ConsoleError(500, "no_bundle")
        from cua.evidence.events import EvidenceEvent  # noqa: PLC0415
        from cua.evidence.writer import RunBundleWriter  # noqa: PLC0415

        writer = RunBundleWriter(self._bundle_dir.name, root=self._bundle_dir.parent)  # re-opening appends
        try:
            writer.json(f"verdicts/{item.id}.json", record)
            writer.event(EvidenceEvent(
                ts=self._clock(), seq=0, run_id=self._bundle_dir.name, actor="human", kind="intervention",
                step_id=item.request.step_id, lease=item.request.lease,
                detail={"action": "resolve_commit", "operator": operator, "intervention": item.id, "ok": True,
                        "verdict": verdict, "record": f"verdicts/{item.id}.json", "mode": "review"},
            ))
        finally:
            writer.close(status="closed")

    # ---------------------------------------------------------------------------------- the human's capture

    @property
    def human_active(self) -> bool:
        """True while a human holds the lease: pass as `active` to `install_capture` and `RequestLog`."""
        return self.lease is not None and self.lease.holder.startswith("human:")

    def _current_handoff(self) -> HandoffRecord | None:
        open_items = self.open_interventions()
        return open_items[-1].handoff if open_items else None

    def capture_sink(self) -> Callable[[CaptureEvent], None]:
        """An `on_event` for `install_capture`: logs each human action as evidence (values already redacted) and
        feeds the current handoff's resume bookkeeping."""
        assert self.writer is not None and self.lease is not None
        log = evidence_sink(self.writer, self.lease.token)

        def sink(event: CaptureEvent) -> None:
            handoff = self._current_handoff()
            if handoff is not None:
                handoff.note_human_action(event)
            log(event)

        return sink

    def request_sink(self) -> Callable[[RequestLogEntry], None]:
        """An `on_commit` for `RequestLog`: logs a `commit_attempt` (actor human) and counts the hit against the
        current handoff, which forbids a resume."""
        assert self.writer is not None and self.lease is not None
        log = commit_hit_sink(self.writer, self.lease.token)

        def sink(entry: RequestLogEntry) -> None:
            handoff = self._current_handoff()
            if handoff is not None:
                handoff.note_request(entry)
            log(entry)

        return sink

    # ------------------------------------------------------------------------------------------------ notify

    def escalated_line(self, item: Escalation) -> dict[str, Any]:
        r = item.request
        return {
            "event": "escalated", "run_id": r.run_id, "intervention_id": r.id, "reason": r.reason,
            "capability": r.capability, "step_id": r.step_id, "lease": r.lease.model_dump(),
            "sla_deadline": r.sla_deadline.isoformat() if r.sla_deadline else None,
            "console": None if self.console_url is None else f"{self.console_url}/interventions/{r.id}",
        }

    def _notify(self, item: Escalation) -> None:
        if self.notify is not None:
            with contextlib.suppress(Exception):
                self.notify(self.escalated_line(item))


# ----------------------------------------------------------------------------------------------------- the web app


class ActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    operator: str
    action_hash: str | None = None
    verdict: str | None = None
    note: str = Field(default="", max_length=500)


def _check_host(value: str | None) -> bool:
    if not value:
        return False
    host = value.rsplit(":", 1)[0] if value.count(":") == 1 or value.startswith("[") and "]:" in value else value
    return host in ALLOWED_HOSTNAMES


def _page(title: str, body: str, *, refresh: int | None = None) -> HTMLResponse:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return HTMLResponse(
        f"<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>{html.escape(title)}</title>{meta}"
        "<style>body{font:14px system-ui,sans-serif;margin:1.5rem;max-width:62rem}table{border-collapse:collapse}"
        "td,th{border:1px solid #bbb;padding:.3rem .6rem;text-align:left}code{background:#eee;padding:0 .2rem}"
        "button{margin:.2rem .3rem .2rem 0}img{max-width:100%;border:1px solid #888}.bad{color:#a00}.ok{color:#070}"
        "</style></head><body>"
        "<p>Operator id: <input id=\"op\" size=\"16\" placeholder=\"your id\"> "
        "<small>local demo console: no authentication, the id is only a label</small></p>"
        f"{body}"
        "<script>const op=document.getElementById('op');op.value=localStorage.getItem('cua_operator')||'';"
        "async function act(path,extra){const o=op.value.trim();localStorage.setItem('cua_operator',o);"
        "const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},"
        "body:JSON.stringify(Object.assign({operator:o},extra||{}))});const j=await r.json();"
        "document.getElementById('msg').textContent=j.ok?('done: '+j.action):(j.code+': '+j.message);"
        "setTimeout(()=>location.reload(),700);}</script></body></html>"
    )


def _queue_html(hub: InterventionHub) -> str:
    rows = "".join(
        f"<tr><td><a href=\"/interventions/{html.escape(r['id'])}\">{html.escape(r['id'])}</a></td>"
        f"<td>{html.escape(r['status'])}</td><td>{html.escape(r['reason'])}</td><td>{html.escape(r['capability'])}</td>"
        f"<td>{html.escape(r['step_id'] or '')}</td><td>{html.escape(r['url'])}</td>"
        f"<td>{html.escape(r['sla_deadline'] or '')}</td></tr>"
        for r in hub.queue()
    )
    table = (
        "<table><tr><th>intervention</th><th>status</th><th>reason</th><th>capability</th><th>step</th><th>url</th>"
        f"<th>claim by</th></tr>{rows}</table>" if rows else "<p>No interventions.</p>"
    )
    return f"<h1>Interventions for run {html.escape(hub.run_id)}</h1>{table}"


def _detail_html(c: dict[str, Any]) -> str:
    e = html.escape
    lease = c["lease"]
    lease_text = "(bundle review: no live lease)" if lease is None else f"{e(lease['holder'])} generation {lease['generation']}"
    esc = c["escalated_lease"]
    shot = f'<p><img src="/interventions/{e(c["id"])}/screenshot" alt="masked screenshot"></p>' if c["screenshot"] else ""
    resume = c["resume"]
    verdict_ui = ""
    if c["accepts_verdict"]:
        verdict_ui = (
            "<p>Verdict: "
            + "".join(f'<button onclick="act(\'/api/interventions/{e(c["id"])}/verdict\',{{verdict:\'{v}\'}})">{v}</button>'
                      for v in VERDICTS)
            + f" (recorded: {e(c['verdict'] or 'none')})</p>"
        )
    base = f"/api/interventions/{e(c['id'])}"
    buttons = "".join(
        f'<button onclick="act(\'{base}/{a}\')">{a}</button>'
        for a, show in (("claim", True), ("resume", True), ("abort", True), ("approve", c["can_approve"]),
                        ("mark-complete", c["can_mark_complete"])) if show
    )
    return (
        f"<p><a href=\"/\">queue</a></p><h1>{e(c['id'])} <small>({e(c['status'])})</small></h1>"
        f"<table><tr><th>reason</th><td>{e(c['reason'])} {e(c['detail'])}</td></tr>"
        f"<tr><th>capability</th><td>{e(c['capability'])} {e(c['version'] or '')} step {e(c['step_id'] or '-')}</td></tr>"
        f"<tr><th>classified state</th><td>{e(c['classified_state'] or '-')}</td></tr>"
        f"<tr><th>expected state</th><td>{e(c['expected_state'] or '-')}</td></tr>"
        f"<tr><th>url</th><td>{e(c['url'])}</td></tr>"
        f"<tr><th>effect so far</th><td>{e(c['effect_so_far'])}"
        f"{' (a commit was attempted)' if c['commit_attempted'] else ''}</td></tr>"
        f"<tr><th>lease now</th><td>{lease_text}</td></tr>"
        f"<tr><th>escalated under</th><td>{e(esc['holder'])} generation {esc['generation']}</td></tr>"
        f"<tr><th>claim by (SLA, unclaimed only)</th><td>{e(c['sla_deadline'] or 'none')}</td></tr>"
        f"<tr><th>resume</th><td class=\"{'ok' if resume['allowed'] else 'bad'}\">"
        f"{'allowed' if resume['allowed'] else 'refused'}: {e(resume['message'])}</td></tr></table>"
        f"{shot}<p>{buttons}</p>{verdict_ui}<p id=\"msg\"></p>"
    )


def build_app(hub: InterventionHub) -> FastAPI:
    app = FastAPI(title="cua operator console", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def local_only(request: Request, call_next):  # noqa: ANN001, ANN202
        if not _check_host(request.headers.get("host")):
            return JSONResponse({"ok": False, "code": "bad_host", "message": "loopback hosts only"}, status_code=403)
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD") and origin is not None:
            host = re.sub(r"^[a-z]+://", "", origin)
            if not _check_host(host) or host != request.headers.get("host"):
                return JSONResponse({"ok": False, "code": "bad_origin", "message": "cross-origin request"},
                                    status_code=403)
        return await call_next(request)

    @app.exception_handler(ConsoleError)
    async def console_error(_: Request, exc: ConsoleError) -> JSONResponse:
        return JSONResponse({"ok": False, "code": exc.code, "message": exc.message}, status_code=exc.status)

    def checked_id(intervention_id: str) -> str:
        if not _ID.fullmatch(intervention_id):
            raise ConsoleError(404, "unknown_intervention")
        return intervention_id

    @app.get("/", response_class=HTMLResponse)
    async def queue_page() -> HTMLResponse:
        hub.tick()
        return _page("cua console", _queue_html(hub), refresh=3)

    @app.get("/interventions/{intervention_id}", response_class=HTMLResponse)
    async def detail_page(intervention_id: str) -> HTMLResponse:
        hub.tick()
        return _page(intervention_id, _detail_html(hub.context(checked_id(intervention_id))), refresh=5)

    @app.get("/interventions/{intervention_id}/screenshot")
    async def screenshot(intervention_id: str) -> Response:
        return FileResponse(hub.screenshot(checked_id(intervention_id)), media_type="image/png",
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/interventions")
    async def api_queue() -> JSONResponse:
        hub.tick()
        return JSONResponse({"run_id": hub.run_id, "live": hub.live, "interventions": hub.queue()})

    @app.get("/api/interventions/{intervention_id}")
    async def api_context(intervention_id: str) -> JSONResponse:
        hub.tick()
        return JSONResponse(hub.context(checked_id(intervention_id)))

    @app.post("/api/interventions/{intervention_id}/{action}")
    async def api_action(intervention_id: str, action: str, body: ActionBody) -> JSONResponse:
        iid = checked_id(intervention_id)
        reply: dict[str, Any] = {"ok": True, "action": action}
        if action == "claim":
            token = await hub.claim(iid, body.operator)
            reply["lease"] = token.model_dump()
        elif action in ("resume", "abort", "approve", "mark-complete"):
            if action == "resume":
                outcome = await hub.resume(iid, body.operator)
            elif action == "abort":
                outcome = await hub.abort(iid, body.operator)
            elif action == "approve":
                outcome = await hub.approve(iid, body.operator, expected_action_hash=body.action_hash)
            else:
                outcome = await hub.mark_complete(iid, body.operator)
            reply.update(outcome=outcome.kind, lease=outcome.lease.model_dump())
        elif action == "verdict":
            record = await hub.verdict(iid, body.operator, body.verdict or "", body.note)
            reply["verdict"] = record["verdict"]
        else:
            raise ConsoleError(404, "unknown_action")
        return JSONResponse(reply)

    return app


# ------------------------------------------------------------------------------------------------------ the server


class _NoSignalServer(uvicorn.Server):
    """uvicorn would install its own SIGINT/SIGTERM handlers; `asyncio.run` owns Ctrl+C here (as in spike S2)."""

    @contextlib.contextmanager
    def capture_signals(self):  # noqa: ANN201
        yield


class ConsoleServer:
    """Serves the console for `hub` on loopback, inside the caller's event loop.

        server = ConsoleServer(hub)
        async with server.running() as url:   # starts serving, waits until it listens, stops on exit
            outcome = await hub.escalate(request)

    or `asyncio.create_task(server.serve())` and `await server.wait_started()`. `port=0` picks a free port."""

    def __init__(self, hub: InterventionHub, *, host: str = "127.0.0.1", port: int = 0) -> None:
        if host not in LOOPBACK_HOSTS:
            raise ValueError("the console binds to loopback only (it has no authentication)")
        self.hub = hub
        self.host = host
        self.port = port
        self.app = build_app(hub)
        self.url: str | None = None
        self._server: _NoSignalServer | None = None
        self._started = asyncio.Event()

    async def serve(self) -> None:
        sock = socket.socket(socket.AF_INET6 if ":" in self.host else socket.AF_INET)
        try:
            sock.bind((self.host, self.port))
            self.port = sock.getsockname()[1]
            shown = f"[{self.host}]" if ":" in self.host else self.host
            server = _NoSignalServer(uvicorn.Config(self.app, log_level="warning", lifespan="off"))
            self._server = server
            task = asyncio.create_task(server.serve(sockets=[sock]))
            announce = asyncio.create_task(self._announce(server, f"http://{shown}:{self.port}", task))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                server.should_exit = True
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(task, 10)
                raise
            finally:
                announce.cancel()
        finally:
            sock.close()

    async def _announce(self, server: uvicorn.Server, url: str, task: asyncio.Task[None]) -> None:
        while not server.started and not task.done():
            await asyncio.sleep(0.02)
        if server.started:
            self.url = url
            self.hub.console_url = url
            self._started.set()

    async def wait_started(self, timeout: float = 10.0) -> str:
        await asyncio.wait_for(self._started.wait(), timeout)
        assert self.url is not None
        return self.url

    def close(self) -> None:
        """Ask the server to stop; `serve()` then returns."""
        if self._server is not None:
            self._server.should_exit = True

    @asynccontextmanager
    async def running(self) -> AsyncIterator[str]:
        task = asyncio.create_task(self.serve())
        try:
            waiter = asyncio.create_task(self.wait_started())
            await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if task.done():
                waiter.cancel()
                task.result()  # surfaces the bind error
            yield await waiter
        finally:
            self.close()
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
