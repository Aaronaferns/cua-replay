"""Control lease: who may act on the live session right now.

Owner: Agent C (safety + control). `LeaseToken` and `ControlLease` are shared types; `LeaseManager` is the state machine.

Every automated action carries a `LeaseToken` (holder + generation it believes it holds). The policy guard rejects
the action unless the token matches the current lease exactly. Each transfer increments `generation`, so a decision
made before a handoff cannot act after control has gone to a human and back. Automation never takes control back
from a human; resume and abort are explicit operator actions.

`LeaseManager` is the state machine behind it (in memory, one per run):

    automation --request_human(reason)--> none (escalated, unclaimed) --claim(op)--> human:<op>
    human:<op> --release_to_automation(op)--> automation        (explicit operator action; the only way back)
    automation | none | human:<op> --abort(op)--> none, terminal
    none (unclaimed past the SLA) --expire_unclaimed(now)--> none, terminal, reason "sla_timeout"

Every transition increments `generation` and calls the injected `on_event` with a `LeaseEvent` (the runtime turns it
into an evidence event `kind="lease"`). There is no automation-initiated transition from `none` or `human` back to
automation, and the SLA timeout never applies once a human has claimed.

This module imports only pydantic and the standard library: `safety` and `surface` import it.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AUTOMATION = "automation"
NOBODY = "none"


def human(operator_id: str) -> str:
    return f"human:{operator_id}"


class LeaseToken(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    holder: str
    generation: int = Field(ge=0)


class ControlLease(BaseModel):
    model_config = ConfigDict(extra="forbid")

    holder: str  # "automation" | "human:<id>" | "none"
    generation: int = Field(ge=0)
    reason: str
    since: datetime

    def token(self) -> LeaseToken:
        return LeaseToken(holder=self.holder, generation=self.generation)


# ----------------------------------------------------------------------------------------------- the state machine

Transition = Literal["request_human", "claim", "release_to_automation", "abort"]
StepOutcome = Literal["continue", "aborted"]


class LeaseError(Exception):
    """A transition the state machine does not allow from the current lease. Nothing changed."""


class LeaseEvent(BaseModel):
    """One transition, as logged. `actor` is who caused it: the automation, a human operator or the system (SLA)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    transition: Transition
    actor: Literal["automation", "human", "system"]
    operator: str | None = None
    from_holder: str
    to_holder: str
    from_generation: int = Field(ge=0)
    generation: int = Field(ge=0)
    reason: str
    at: datetime

    def token(self) -> LeaseToken:
        return LeaseToken(holder=self.to_holder, generation=self.generation)


LeaseEventHandler = Callable[[LeaseEvent], None]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class LeaseManager:
    """Who holds the control lease, and the only legal ways it changes hands. In memory.

    `token()` returns the current `LeaseToken`: it is the lease provider, `PolicyGuard(policy, lease=manager.token)`.
    A token taken before a transition no longer matches afterwards, so an in-flight decision cannot act after a handoff.

    `sla_seconds` bounds how long an escalation may sit unclaimed; `expire_unclaimed(now)` enforces it (the caller
    polls). `on_event` is called after each transition, outside the lock; its failures never undo a transition.
    State changes are lock-protected; the step-boundary waits are for tasks on one event loop.
    """

    def __init__(
        self,
        *,
        on_event: LeaseEventHandler | None = None,
        sla_seconds: float | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._lock = threading.RLock()
        self._on_event = on_event
        self._sla = sla_seconds
        self._clock = clock
        self._lease = ControlLease(holder=AUTOMATION, generation=0, reason="run_started", since=clock())
        self._aborted = False
        self._drained: asyncio.Event | None = None
        self._changed: asyncio.Event | None = None
        self.history: list[LeaseEvent] = []

    # ------------------------------------------------------------------------------------------------- reading

    def token(self) -> LeaseToken:
        with self._lock:
            return self._lease.token()

    @property
    def lease(self) -> ControlLease:
        """A snapshot of the current lease (holder, generation, reason, since)."""
        with self._lock:
            return self._lease.model_copy()

    @property
    def holder(self) -> str:
        return self._lease.holder

    @property
    def generation(self) -> int:
        return self._lease.generation

    @property
    def aborted(self) -> bool:
        return self._aborted

    @property
    def pause_requested(self) -> bool:
        """True from the moment control leaves the automation (escalation, human, abort). The automation checks this
        between steps (`step_boundary`) and must not start another step while it is true."""
        return self._lease.holder != AUTOMATION

    # ------------------------------------------------------------------------------------------- transitions

    def request_human(self, reason: str) -> LeaseToken:
        """automation -> none: escalated and unclaimed. The only transition the automation itself may initiate."""
        with self._lock:
            self._require(self._lease.holder == AUTOMATION, "only the automation can request a human")
            event = self._move("request_human", "automation", None, NOBODY, reason)
        self._publish(event)
        return event.token()

    def claim(self, operator: str) -> LeaseToken:
        """none -> human:<operator>: a person takes an escalated, unclaimed run."""
        with self._lock:
            self._require(not self._aborted, "the run was aborted")
            self._require(self._lease.holder == NOBODY, "the lease is not waiting for a human")
            event = self._move("claim", "human", operator, human(operator), "claimed")
        self._publish(event)
        return event.token()

    def release_to_automation(self, operator: str, reason: str = "operator_resume") -> LeaseToken:
        """human:<operator> -> automation. Explicit, and only by the human who holds the lease: never automatic."""
        with self._lock:
            self._require(self._lease.holder == human(operator), "the lease is not held by this operator")
            event = self._move("release_to_automation", "human", operator, AUTOMATION, reason)
        self._publish(event)
        return event.token()

    def abort(self, operator: str | None, reason: str = "operator_abort") -> LeaseToken:
        """Any holder -> none, terminal: nobody may act, and nothing can be claimed or released afterwards."""
        with self._lock:
            event = self._abort(operator, reason, "system" if operator is None else "human")
        self._publish(event)
        return event.token()

    def expire_unclaimed(self, now: datetime | None = None) -> bool:
        """Abort with reason `sla_timeout`, but only while the request is unclaimed (holder `none`) and older than the
        SLA. A claimed or automation-held lease is never expired. Returns whether it aborted."""
        now = now or self._clock()
        with self._lock:
            if self._sla is None or self._aborted or self._lease.holder != NOBODY:
                return False
            if (now - self._lease.since).total_seconds() < self._sla:
                return False
            event = self._abort(None, "sla_timeout", "system")
        self._publish(event)
        return True

    # ------------------------------------------------------------------------------------------- step boundary

    async def step_boundary(self) -> StepOutcome:
        """Await between steps. Returns `"continue"` at once while the automation holds the lease. When control has
        left it, this marks the automation drained (no step in flight: `wait_drained` returns) and then blocks until
        the lease is released back to automation (`"continue"`) or the run is aborted (`"aborted"`). After
        `"continue"` the caller takes a fresh `token()`: the generation changed."""
        if not self.pause_requested:
            await asyncio.sleep(0)  # a boundary is also a scheduling point, so a console task can run
            return "continue"
        self._drained_event().set()
        while True:
            if self._aborted:
                return "aborted"
            if not self.pause_requested:
                return "continue"
            changed = self._changed_event()
            changed.clear()
            await changed.wait()

    @property
    def is_drained(self) -> bool:
        """True once the automation has reached a step boundary after control left it."""
        return self._drained is not None and self._drained.is_set()

    async def wait_drained(self, timeout: float | None = None) -> bool:
        """Await the automation reaching a step boundary (an operator console awaits this before it presents the page
        as the human's). False on timeout."""
        try:
            await asyncio.wait_for(self._drained_event().wait(), timeout)
        except TimeoutError:
            return False
        return True

    # ------------------------------------------------------------------------------------------------ internals

    def _drained_event(self) -> asyncio.Event:
        if self._drained is None:
            self._drained = asyncio.Event()
        return self._drained

    def _changed_event(self) -> asyncio.Event:
        if self._changed is None:
            self._changed = asyncio.Event()
        return self._changed

    @staticmethod
    def _require(condition: bool, message: str) -> None:
        if not condition:
            raise LeaseError(message)

    def _abort(self, operator: str | None, reason: str, actor: Literal["human", "system"]) -> LeaseEvent:
        self._require(not self._aborted, "the run was already aborted")
        event = self._move("abort", actor, operator, NOBODY, reason)
        self._aborted = True
        return event

    def _move(
        self,
        transition: Transition,
        actor: Literal["automation", "human", "system"],
        operator: str | None,
        to_holder: str,
        reason: str,
    ) -> LeaseEvent:
        before = self._lease
        now = self._clock()
        self._lease = ControlLease(holder=to_holder, generation=before.generation + 1, reason=reason, since=now)
        event = LeaseEvent(
            transition=transition,
            actor=actor,
            operator=operator,
            from_holder=before.holder,
            to_holder=to_holder,
            from_generation=before.generation,
            generation=self._lease.generation,
            reason=reason,
            at=now,
        )
        self.history.append(event)
        return event

    def _publish(self, event: LeaseEvent) -> None:
        if event.to_holder == AUTOMATION and self._drained is not None:
            self._drained.clear()  # automation is running again: the next pause must drain afresh
        if self._changed is not None:
            self._changed.set()
        if self._on_event is not None:
            with contextlib.suppress(Exception):  # logging must never undo a transition
                self._on_event(event)
