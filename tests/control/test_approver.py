"""Discovery's console approval: the approver redeems it once at the post-release generation; stale generation refused."""

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


async def test_approver_redeems_its_approval_once(hub):
    approval = await approved(hub)
    assert approval is not None
    assert hub.approvals.consume(approval, action_hash=HASH, generation=hub.lease.generation).reason == "already_used"


def test_approval_for_another_action_or_generation_is_rejected(hub):
    a = hub.approvals.issue(action_hash=HASH, generation=3, operator="ann").approval_id
    assert hub.approvals.consume(a, action_hash="b" * 64, generation=3).reason == "action_mismatch"
    b = hub.approvals.issue(action_hash=HASH, generation=3, operator="ann").approval_id
    assert hub.approvals.consume(b, action_hash=HASH, generation=2).reason == "generation_mismatch"


async def test_stale_generation_gets_no_escalation_and_no_approval(hub):
    assert await console_approver(hub)(HASH, hub.lease.generation + 7) is None
    assert not hub._items
