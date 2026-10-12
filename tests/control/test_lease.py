"""The control-lease state machine: transitions, generations, the SLA rule, the guard integration and the step boundary."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from cua.control.lease import (
    AUTOMATION,
    NOBODY,
    ControlLease,
    LeaseError,
    LeaseEvent,
    LeaseManager,
    LeaseToken,
    human,
)
from cua.safety.policy import ActionRequest, Policy, PolicyGuard

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def events() -> list[LeaseEvent]:
    return []


@pytest.fixture
def manager(clock, events) -> LeaseManager:
    return LeaseManager(on_event=events.append, sla_seconds=300, clock=clock)


POLICY = Policy.model_validate({
    "version": "cua.policy/v1", "name": "lease-test", "origins": ["http://localhost:8087"],
    "routes": {"allow": ["/**"]}, "actions": ["click", "navigate", "extract"], "max_effect": "read",
})


def request(token: LeaseToken) -> ActionRequest:
    return ActionRequest(
        kind="click", actor="replay", page_url="http://localhost:8087/", effect="none", lease=token
    )


# ------------------------------------------------------------------------------------------------ the happy path


def test_starts_with_the_automation_at_generation_zero(manager):
    assert manager.token() == LeaseToken(holder=AUTOMATION, generation=0)
    assert manager.lease.reason == "run_started" and not manager.pause_requested and not manager.aborted


def test_escalation_claim_and_resume_walk_the_lease_and_bump_the_generation(manager):
    assert manager.request_human("HUMAN_ONLY_LOGIN") == LeaseToken(holder=NOBODY, generation=1)
    assert manager.lease.reason == "HUMAN_ONLY_LOGIN" and manager.pause_requested
    assert manager.claim("alice") == LeaseToken(holder=human("alice"), generation=2)
    assert manager.release_to_automation("alice") == LeaseToken(holder=AUTOMATION, generation=3)
    assert not manager.pause_requested
    assert isinstance(manager.lease, ControlLease) and manager.lease.token() == manager.token()


def test_transitions_are_logged_with_before_and_after(manager, events, clock):
    clock.advance(5)
    manager.request_human("captcha")
    clock.advance(20)
    manager.claim("alice")
    clock.advance(60)
    manager.release_to_automation("alice")
    clock.advance(1)
    manager.request_human("again")
    manager.abort("bob")

    assert events == manager.history
    assert [(e.transition, e.actor, e.operator) for e in events] == [
        ("request_human", "automation", None),
        ("claim", "human", "alice"),
        ("release_to_automation", "human", "alice"),
        ("request_human", "automation", None),
        ("abort", "human", "bob"),
    ]
    assert [(e.from_holder, e.to_holder) for e in events] == [
        (AUTOMATION, NOBODY), (NOBODY, "human:alice"), ("human:alice", AUTOMATION), (AUTOMATION, NOBODY), (NOBODY, NOBODY),
    ]
    assert [e.generation for e in events] == [1, 2, 3, 4, 5]  # strictly +1 per transition
    assert all(e.generation == e.from_generation + 1 for e in events)
    assert events[0].at == T0 + timedelta(seconds=5) and events[0].reason == "captcha"
    assert events[2].token() == manager.history[2].token() == LeaseToken(holder=AUTOMATION, generation=3)


def test_refused_transitions_change_nothing_and_log_nothing(manager, events):
    for refused in (
        lambda: manager.claim("alice"),  # nothing was requested
        lambda: manager.release_to_automation("alice"),  # no human holds it
    ):
        with pytest.raises(LeaseError):
            refused()
    manager.request_human("x")
    with pytest.raises(LeaseError):
        manager.request_human("y")  # already escalated
    manager.claim("alice")
    with pytest.raises(LeaseError):
        manager.claim("bob")  # taken
    with pytest.raises(LeaseError):
        manager.release_to_automation("bob")  # only the holder resumes
    with pytest.raises(LeaseError):
        manager.request_human("z")  # a human holds it
    assert manager.token() == LeaseToken(holder="human:alice", generation=2) and len(events) == 2


def test_a_failing_event_handler_does_not_undo_the_transition(clock):
    def boom(event: LeaseEvent) -> None:
        raise RuntimeError("evidence store down")

    manager = LeaseManager(on_event=boom, clock=clock)
    assert manager.request_human("x").generation == 1 and manager.holder == NOBODY


# ------------------------------------------------------------------------------------------ the guard integration


def test_stale_generation_cannot_act(manager):
    guard = PolicyGuard(POLICY, lease=manager.token)
    before = manager.token()
    assert guard.check(request(before)).allowed

    manager.request_human("captcha")
    manager.claim("alice")
    manager.release_to_automation("alice")  # automation holds it again, but at a new generation
    assert manager.holder == AUTOMATION

    stale = guard.check(request(before))
    assert not stale.allowed and stale.rule == "lease_generation"
    assert guard.check(request(manager.token())).allowed  # a decision made after the handoff is fine


def test_automation_is_denied_while_escalated_or_with_a_human(manager):
    guard = PolicyGuard(POLICY, lease=manager.token)
    manager.request_human("captcha")
    assert guard.check(request(LeaseToken(holder=AUTOMATION, generation=1))).rule == "lease_generation"
    assert guard.check(request(manager.token())).rule == "lease_holder"  # even a token naming the current holder
    manager.claim("alice")
    assert guard.check(request(manager.token())).rule == "lease_holder"


def test_automation_never_reclaims_from_human(manager):
    manager.request_human("captcha")
    manager.claim("alice")
    # the API has no automation-side way back: every transition that ends in `automation` is an operator action...
    public = {n for n in dir(LeaseManager) if not n.startswith("_") and callable(getattr(LeaseManager, n))}
    assert public == {
        "token", "request_human", "claim", "release_to_automation", "abort", "expire_unclaimed", "step_boundary",
        "wait_drained",
    }
    # ...and none of the others can move the lease off a human
    with pytest.raises(LeaseError):
        manager.request_human("take it back")
    assert manager.expire_unclaimed(T0 + timedelta(days=1)) is False
    with pytest.raises(LeaseError):
        manager.release_to_automation("automation")
    with pytest.raises(LeaseError):
        manager.release_to_automation("bob")
    assert manager.holder == "human:alice" and manager.generation == 2

    # even the (stale) automation token cannot act, and the human still holds the lease
    guard = PolicyGuard(POLICY, lease=manager.token)
    assert not guard.check(request(LeaseToken(holder=AUTOMATION, generation=0))).allowed
    assert manager.holder == "human:alice"


def test_nothing_returns_to_automation_from_none_without_a_human(manager):
    manager.request_human("captcha")
    with pytest.raises(LeaseError):
        manager.release_to_automation("alice")
    assert manager.holder == NOBODY


# ------------------------------------------------------------------------------------------------------- the SLA


def test_sla_only_for_unclaimed(manager, events, clock):
    # automation holds: never expires, however long
    assert manager.expire_unclaimed(T0 + timedelta(days=30)) is False and manager.holder == AUTOMATION

    # unclaimed and inside the SLA: nothing
    manager.request_human("captcha")
    assert manager.expire_unclaimed(clock.advance(299)) is False and manager.holder == NOBODY

    # unclaimed past the SLA: abort with reason sla_timeout, logged as a system action
    assert manager.expire_unclaimed(clock.advance(2)) is True
    assert manager.aborted and manager.holder == NOBODY and manager.lease.reason == "sla_timeout"
    assert (events[-1].transition, events[-1].actor, events[-1].reason) == ("abort", "system", "sla_timeout")
    assert manager.expire_unclaimed(clock.advance(1000)) is False  # already terminal: not again
    with pytest.raises(LeaseError):
        manager.claim("alice")  # too late


def test_sla_never_expires_a_claimed_lease(clock, events):
    manager = LeaseManager(on_event=events.append, sla_seconds=60, clock=clock)
    manager.request_human("captcha")
    clock.advance(30)
    manager.claim("alice")
    assert manager.expire_unclaimed(clock.advance(86_400)) is False
    assert manager.holder == "human:alice" and not manager.aborted
    assert "sla_timeout" not in [e.reason for e in events]


def test_without_an_sla_nothing_expires(clock):
    manager = LeaseManager(clock=clock)
    manager.request_human("captcha")
    assert manager.expire_unclaimed(clock.advance(10**6)) is False


def test_abort_is_terminal_from_every_holder(clock):
    for setup in (
        lambda m: None,
        lambda m: m.request_human("x"),
        lambda m: (m.request_human("x"), m.claim("alice")),
    ):
        manager = LeaseManager(clock=clock)
        setup(manager)
        token = manager.abort("bob")
        assert token.holder == NOBODY and manager.aborted and manager.pause_requested
        for refused in (
            lambda m=manager: m.abort("bob"),
            lambda m=manager: m.claim("alice"),
            lambda m=manager: m.release_to_automation("alice"),
            lambda m=manager: m.request_human("again"),
        ):
            with pytest.raises(LeaseError):
                refused()


# ------------------------------------------------------------------------------------------------ the step boundary


async def test_step_boundary_continues_while_the_automation_holds_the_lease(manager):
    assert await manager.step_boundary() == "continue"
    assert not manager.is_drained


async def test_step_boundary_drains_then_waits_for_the_operator_to_resume(manager):
    steps: list[str] = []

    async def interpreter() -> str:
        for step in ("s1", "s2", "s3", "s4"):
            if await manager.step_boundary() == "aborted":
                return "aborted"
            steps.append(step)
            await asyncio.sleep(0.01)
        return "done"

    task = asyncio.create_task(interpreter())
    await asyncio.sleep(0.03)
    manager.request_human("captcha")
    assert manager.pause_requested  # the interpreter drains at its next boundary
    assert await manager.wait_drained(timeout=1)  # the in-flight step finished
    done = list(steps)
    manager.claim("alice")
    await asyncio.sleep(0.05)
    assert steps == done and not task.done()  # no step ran while the human held the lease

    manager.release_to_automation("alice")
    assert await asyncio.wait_for(task, 1) == "done" and steps == ["s1", "s2", "s3", "s4"]
    assert not manager.is_drained  # automation is running again


async def test_step_boundary_reports_an_abort(manager):
    async def interpreter() -> str:
        return await manager.step_boundary()

    manager.request_human("captcha")
    task = asyncio.create_task(interpreter())
    assert await manager.wait_drained(timeout=1)
    manager.abort("bob")
    assert await asyncio.wait_for(task, 1) == "aborted"


async def test_wait_drained_times_out_when_nobody_reaches_a_boundary(manager):
    manager.request_human("captcha")
    assert await manager.wait_drained(timeout=0.05) is False
