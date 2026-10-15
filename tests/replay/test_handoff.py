"""M5: a CAPTCHA login escalates to a person through the real InterventionHub (offline, against the fake)."""

from __future__ import annotations

import asyncio
import json

import pytest
from fakes import BASE, AllowGuard, Clock, FakeDolibarr
from sample_docs import capability, dolibarr_profile

from cua.control.console import InterventionHub
from cua.control.lease import LeaseManager
from cua.evidence.writer import RunBundleWriter
from cua.replay.interpreter import Credentials, replay

CODE = "CU1212-0005"
CARD = f"{BASE}/societe/card.php?code={CODE}"


@pytest.fixture
def fake() -> FakeDolibarr:
    f = FakeDolibarr(AllowGuard())
    f.logged_in, f.captcha = False, True
    return f


async def handoff(fake, tmp_path, human):
    """Replay against a real hub; `human(hub, intervention_id)` plays the operator once the request is claimable."""
    writer = RunBundleWriter("run-m5", root=tmp_path)
    mgr = LeaseManager()
    hub = InterventionHub(run_id="run-m5", writer=writer, lease=mgr, poll_seconds=0.01)

    async def operator():
        while not hub.open_interventions():
            await asyncio.sleep(0.01)
        iid = hub.open_interventions()[0].id
        await mgr.wait_drained(5)
        await hub.claim(iid, "ann")
        await human(hub, iid)

    task = asyncio.create_task(operator())
    clock = Clock()
    try:
        result = await asyncio.wait_for(replay(
            capability(), dolibarr_profile(), {"customer_code": CODE}, surface=fake, guard=fake.guard,
            lease=mgr.token, base_url=BASE, credentials=Credentials("admin", "pw"), run_id="run-m5",
            now_ms=clock.now, sleep=clock.sleep, hub=hub, evidence=writer), 10)
        await task
    finally:
        writer.close(status="closed")
    events = [json.loads(x) for x in (writer.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    return result, events, mgr


async def test_captcha_login_escalates_and_resume_restarts_on_the_same_session(fake, tmp_path):
    async def human(hub, iid):
        fake.captcha, fake.logged_in = False, True  # the person solves the CAPTCHA in the live page
        await hub.resume(iid, "ann")

    r, events, mgr = await handoff(fake, tmp_path, human)
    assert r.status == "success" and r.outputs == {"outstanding_amount": "75.37"}
    assert not any(q.actor == "handler" for q in fake.guard.requests)  # automation never typed the code
    kinds = [e["kind"] for e in events]
    assert kinds.index("escalated") < kinds.index("handoff_ended")
    assert mgr.token().holder == "automation" and mgr.token().generation >= 2


async def test_mark_complete_finishes_through_checkpoint_and_extraction(fake, tmp_path):
    async def human(hub, iid):
        fake.captcha, fake.logged_in, fake.url = False, True, CARD  # the person finished the lookup by hand
        await hub.mark_complete(iid, "ann")

    r, _, _ = await handoff(fake, tmp_path, human)
    assert r.status == "success" and r.outputs == {"outstanding_amount": "75.37"}


async def test_abort_during_handoff_is_a_control_failure(fake, tmp_path):
    async def human(hub, iid):
        await hub.abort(iid, "ann")

    r, _, _ = await handoff(fake, tmp_path, human)
    assert r.status == "failure" and r.failure.code == "RUN_ABORTED" and r.failure.category == "control"
