"""The resume rules: when a run may restart after a human handoff, and when a human may declare it complete.

Owner: Agent C. Pure functions and one small record; no I/O, no browser. The console (`cua.control.console`) applies
them; the interpreter (B) feeds them the facts it alone knows.

Resume means "restart from the entry on the same session". It is allowed at most once per handoff, and never when
a person or the automation may already have committed: a restart replays steps, and a replayed commit is the one
mistake this system must not make. So `decide_resume` refuses when ANY of these holds, checked in this order:

    commit_attempted_by_automation  the run recorded a `commit_attempt` marker before it stopped
    human_commit_click              a captured human click, Enter or submit on a commit control
    request_log_commit              the human's request log shows a hit on a profile commit route
    effect_above_read               the run's effect high-water mark is above `read` (that is, `commit`)
    already_resumed                 this handoff was already resumed once

and allows (`ok`) otherwise. A refusal leaves the operator two exits: abort, or mark-complete.

Mark-complete is not a trust-the-human button. `decide_mark_complete` allows it only when the capability's success
checkpoint holds on the live page AND the declared outputs can be extracted from it; the interpreter supplies both
as callbacks (this module never touches a page), and whatever they extract is returned to the caller, never logged
here. A callback that raises counts as "not satisfied" (the message is dropped: it may carry page text).

`HandoffRecord` is the per-handoff bookkeeping (one per intervention): the high-water effect, the commit signals seen
while a human held the lease, and how many times the handoff was resumed.

Classifying a captured human action as "a click on a commit control" is conservative. The capture reports only the
shape of the action (tag, type, a click's own label, a submit's method, target path and field names), never values.
`is_commit_control` flags a submit whose method and target path match a commit route whose required form fields are
all present, and a click whose label carries a commit word (validate, confirm, pay, delete, ...). A false positive
refuses a resume (the safe side); the request log (`RequestLog.commit_hits`) is the exact backstop for the rest.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

from cua.artifact.predicates import glob_match
from cua.artifact.schema import EFFECT_RANK, Effect, Profile

if TYPE_CHECKING:
    from cua.control.capture import CaptureEvent, RequestLogEntry

ResumeReason = Literal[
    "ok",
    "commit_attempted_by_automation",
    "human_commit_click",
    "request_log_commit",
    "effect_above_read",
    "already_resumed",
]
MarkCompleteReason = Literal[
    "ok", "no_checkpoint_available", "checkpoint_not_satisfied", "outputs_not_extractable"
]

_MESSAGES: dict[str, str] = {
    "ok": "a restart from the entry is allowed (once)",
    "commit_attempted_by_automation": "the automation attempted a commit before it stopped; a restart could repeat it",
    "human_commit_click": "the operator acted on a commit control; a restart could repeat the commit",
    "request_log_commit": "the operator's session sent a request to a commit route; a restart could repeat it",
    "effect_above_read": "the run already reached an effect above read; only abort or mark-complete remain",
    "already_resumed": "this handoff was already resumed once",
}


@dataclass(frozen=True)
class ResumeState:
    """Everything `decide_resume` looks at."""

    high_water_effect: Effect = "none"
    commit_attempted: bool = False  # the automation's `commit_attempt` marker
    human_commit_clicks: int = 0  # captured human actions on commit controls
    request_log_commit_hits: int = 0  # `RequestLog.commit_hits` while a human held the lease
    resumes_used: int = 0  # resumes already granted in this handoff


@dataclass(frozen=True)
class ResumeDecision:
    allowed: bool
    reason: ResumeReason
    message: str = ""


def _resume(allowed: bool, reason: ResumeReason) -> ResumeDecision:
    return ResumeDecision(allowed=allowed, reason=reason, message=_MESSAGES[reason])


def decide_resume(state: ResumeState) -> ResumeDecision:
    if state.commit_attempted:
        return _resume(False, "commit_attempted_by_automation")
    if state.human_commit_clicks > 0:
        return _resume(False, "human_commit_click")
    if state.request_log_commit_hits > 0:
        return _resume(False, "request_log_commit")
    if EFFECT_RANK[state.high_water_effect] > EFFECT_RANK["read"]:
        return _resume(False, "effect_above_read")
    if state.resumes_used >= 1:
        return _resume(False, "already_resumed")
    return _resume(True, "ok")


# ------------------------------------------------------------------------------------------------- mark-complete


class CheckpointEvaluator(Protocol):
    def __call__(self) -> bool | Awaitable[bool]: ...


class OutputExtractor(Protocol):
    def __call__(self) -> Mapping[str, Any] | None | Awaitable[Mapping[str, Any] | None]: ...


@dataclass(frozen=True)
class MarkCompleteDecision:
    allowed: bool
    reason: MarkCompleteReason
    outputs: dict[str, Any] | None = None  # the extracted outputs, only when allowed; value-bearing: do not log

    @property
    def message(self) -> str:
        return {
            "ok": "the success checkpoint holds and the outputs were extracted",
            "no_checkpoint_available": "this run has no success checkpoint to verify; mark-complete is refused",
            "checkpoint_not_satisfied": "the page does not satisfy the capability's success checkpoint",
            "outputs_not_extractable": "the success checkpoint holds but the declared outputs could not be extracted",
        }[self.reason]


async def _call(fn: Callable[[], Any]) -> Any:
    result = fn()
    return await result if inspect.isawaitable(result) else result


async def decide_mark_complete(
    *,
    checkpoint: CheckpointEvaluator | None,
    extract_outputs: OutputExtractor | None,
) -> MarkCompleteDecision:
    """Allowed only if `checkpoint()` is true and `extract_outputs()` returns the outputs (a mapping, possibly empty
    for a capability that declares none; `None` means "could not extract"). Both are the interpreter's, and run on the
    live page. Missing callbacks refuse: there is no way to mark a run complete without them."""
    if checkpoint is None or extract_outputs is None:
        return MarkCompleteDecision(False, "no_checkpoint_available")
    try:
        satisfied = bool(await _call(checkpoint))
    except Exception:  # noqa: BLE001 - the message may carry page text
        satisfied = False
    if not satisfied:
        return MarkCompleteDecision(False, "checkpoint_not_satisfied")
    try:
        outputs = await _call(extract_outputs)
    except Exception:  # noqa: BLE001
        outputs = None
    if outputs is None:
        return MarkCompleteDecision(False, "outputs_not_extractable")
    return MarkCompleteDecision(True, "ok", outputs=dict(outputs))


# ---------------------------------------------------------------------------------------------- commit classification

_COMMIT_WORDS = re.compile(r"\b(validate|confirm|pay|payment|paiement|delete|remove|approve)\b", re.IGNORECASE)


def _submit_matches_commit_route(event: CaptureEvent, profile: Profile) -> bool:
    method = (event.method or "get").upper()
    fields = set(event.fields)
    for route in profile.commit_routes:
        if route.method != "ANY" and route.method != method:
            continue
        if not event.action or not glob_match(route.path, event.action):
            continue
        # a required form field the form does not even have cannot match. A GET form's fields are its query
        # parameters; a POST form's query comes from its action URL, which the capture does not keep: assume it matches.
        if set(route.body) <= fields and (method != "GET" or set(route.query) <= fields):
            return True
    return False


def is_commit_control(event: CaptureEvent, profile: Profile | None) -> bool:
    """Conservative: could this captured human action have committed? See the module docstring.

    A `submit` is judged against the profile's commit routes (every form submission, whether by click or by Enter,
    fires one). A `click` on a link, button or input is judged by its own label (a commit word raises it: the
    regex may add, never lower). Without a profile nothing can be ruled out, so every submit counts."""
    if event.kind == "submit":
        return True if profile is None else _submit_matches_commit_route(event, profile)
    if event.kind == "click":
        label = " ".join(x for x in (event.text, event.label, event.name) if x)
        return bool(_COMMIT_WORDS.search(label))
    return False


# --------------------------------------------------------------------------------------------------- the handoff


class HandoffRecord:
    """One handoff's resume bookkeeping. Created when the run escalates; the console feeds it the human's captured
    actions and request-log hits; `consume_resume()` is how a resume is granted (at most once)."""

    def __init__(
        self,
        handoff_id: str,
        *,
        high_water_effect: Effect = "none",
        commit_attempted: bool = False,
        profile: Profile | None = None,
    ) -> None:
        self.handoff_id = handoff_id
        self.high_water_effect: Effect = high_water_effect
        self.commit_attempted = commit_attempted
        self.human_commit_clicks = 0
        self.request_log_commit_hits = 0
        self.resumes = 0
        self._profile = profile

    def note_effect(self, effect: Effect) -> None:
        if EFFECT_RANK[effect] > EFFECT_RANK[self.high_water_effect]:
            self.high_water_effect = effect

    def note_automation_commit(self) -> None:
        self.commit_attempted = True
        self.note_effect("commit")

    def note_human_action(self, event: CaptureEvent) -> bool:
        """Record a captured human action; True when it counted as a possible commit."""
        if is_commit_control(event, self._profile):
            self.human_commit_clicks += 1
            return True
        return False

    def note_request(self, entry: RequestLogEntry) -> bool:
        """Record a logged human request; True when it hit a commit route."""
        if entry.commit:
            self.request_log_commit_hits += 1
            return True
        return False

    def state(self) -> ResumeState:
        return ResumeState(
            high_water_effect=self.high_water_effect,
            commit_attempted=self.commit_attempted,
            human_commit_clicks=self.human_commit_clicks,
            request_log_commit_hits=self.request_log_commit_hits,
            resumes_used=self.resumes,
        )

    def decide(self) -> ResumeDecision:
        return decide_resume(self.state())

    def consume_resume(self) -> ResumeDecision:
        """Decide, and when allowed count the resume so that a second one in this handoff is refused."""
        decision = self.decide()
        if decision.allowed:
            self.resumes += 1
        return decision
