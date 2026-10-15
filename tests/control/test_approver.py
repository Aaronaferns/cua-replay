"""Discovery's console approval: bound to (action hash, generation), single use, stale generation refused."""

from __future__ import annotations

import asyncio

import pytest

from cua.control.approver import console_approver
from cua.control.console import InterventionHub
from cua.control.lease import LeaseManager
from cua.evidence.writer import RunBundleWriter

HASH = "a" * 64


@pytest.fixture
def hub(tmp_path):
    w = RunBundleWriter("run-ap", root=tmp_path)
    yield InterventionHub(run_id="run-ap", writer=w, lease=LeaseManager(), poll_seconds=0.01)
    w.close(status="closed")


async def approved(hub, approver=None):
    task = asyncio.create_task((approver or console_approver(hub))(HASH, hub.lease.generation))
    await asyncio.sleep(0.05)
    iid = [i for i, it in hub._items.items() if it.outcome is None][0]
    await hub.claim(iid, "ann")
    await hub.approve(iid, "ann", expected_action_hash=HASH)
    return await asyncio.wait_for(task, 2)


async def test_approval_is_single_use(hub):
    approval = await approved(hub)
    assert approval is not None
    gen = hub.lease.generation
    assert hub.approvals.consume(approval, action_hash=HASH, generation=gen).ok
    assert hub.approvals.consume(approval, action_hash=HASH, generation=gen).reason == "already_used"


async def test_approval_for_another_action_or_generation_is_rejected(hub):
    approver = console_approver(hub)
    a = await approved(hub, approver)
    assert hub.approvals.consume(a, action_hash="b" * 64, generation=hub.lease.generation).reason == "action_mismatch"
    b = await approved(hub, approver)
    assert hub.approvals.consume(b, action_hash=HASH, generation=hub.lease.generation - 1).reason == "generation_mismatch"


async def test_stale_generation_gets_no_escalation_and_no_approval(hub):
    assert await console_approver(hub)(HASH, hub.lease.generation + 7) is None
    assert not hub._items
