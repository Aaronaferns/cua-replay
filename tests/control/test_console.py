"""The operator console end to end over ASGI: queue, claim, resume, abort, approve, mark-complete, verdict."""

from __future__ import annotations

import argparse
import asyncio

import httpx2 as httpx
import pytest

from cua.control.commands import register
from cua.control.console import InterventionHub, build_app
from cua.control.interventions import COMMIT_UNKNOWN, HUMAN_ONLY_LOGIN, InterventionRequest
from cua.control.lease import AUTOMATION, LeaseManager
from cua.evidence.writer import RunBundleWriter

RUN = "run-c1"


@pytest.fixture
def writer(tmp_path):
    w = RunBundleWriter(RUN, root=tmp_path)
    yield w
    w.close(status="closed")


@pytest.fixture
def hub(writer):
    return InterventionHub(run_id=RUN, writer=writer, lease=LeaseManager(), poll_seconds=0.01)


@pytest.fixture
async def api(hub):
    transport = httpx.ASGITransport(app=build_app(hub))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as c:
        yield c


def req(hub, iid="i1", reason=HUMAN_ONLY_LOGIN, **kw):
    return InterventionRequest.create(
        id=iid, run_id=RUN, capability="dolibarr.lookup", reason=reason, url="http://localhost:8087/index.php",
        lease=hub.lease.token(), step_id="s1", **{"effect_so_far": "read", **kw},
    )


async def act(api, iid, action, **body):
    return await api.post(f"/api/interventions/{iid}/{action}", json={"operator": "ann", **body})


async def settle(task):
    return await asyncio.wait_for(task, 5)


async def test_pause_before_request_visible(hub, api):
    hub.register(req(hub))  # lease moved to none, but the automation has not drained yet
    rows = (await api.get("/api/interventions")).json()["interventions"]
    assert rows[0]["status"] == "draining"
    r = await act(api, "i1", "claim")
    assert r.status_code == 409 and r.json()["code"] == "not_drained"
    assert hub.lease.holder == "none"


async def test_drained_request_becomes_claimable(hub, api):
    task = asyncio.create_task(hub.escalate(req(hub)))
    await asyncio.sleep(0.05)
    assert (await api.get("/api/interventions")).json()["interventions"][0]["status"] == "open"
    assert (await act(api, "i1", "claim")).status_code == 200
    await act(api, "i1", "abort")
    assert (await settle(task)).kind == "abort"


async def test_claim_resume_once_and_automation_gets_a_new_generation(hub, api):
    task = asyncio.create_task(hub.escalate(req(hub)))
    await asyncio.sleep(0.05)
    assert (await act(api, "i1", "resume")).status_code == 409  # not claimed by this operator
    assert (await act(api, "i1", "claim")).json()["lease"]["holder"] == "human:ann"
    assert (await api.get("/api/interventions/i1")).status_code == 200
    assert (await act(api, "i1", "resume")).json()["outcome"] == "resume"
    out = await settle(task)
    assert hub.lease.holder == AUTOMATION and out.lease.generation == hub.lease.generation


async def test_resume_refused_after_a_commit_attempt(hub, api):
    task = asyncio.create_task(hub.escalate(req(hub, commit_attempted=True, effect_so_far="commit")))
    await asyncio.sleep(0.05)
    await act(api, "i1", "claim")
    r = await act(api, "i1", "resume")
    assert r.status_code == 409 and hub.lease.holder == "human:ann"
    await act(api, "i1", "abort")
    assert (await settle(task)).kind == "abort"


async def test_mark_complete_requires_checkpoint_through_the_console(hub, api):
    ok = {"v": False}
    task = asyncio.create_task(
        hub.escalate(req(hub), checkpoint=lambda: ok["v"], extract_outputs=lambda: {"amount": "1"})
    )
    await asyncio.sleep(0.05)
    await act(api, "i1", "claim")
    assert (await act(api, "i1", "mark-complete")).status_code == 409
    ok["v"] = True
    assert (await act(api, "i1", "mark-complete")).json()["outcome"] == "mark_complete"
    assert (await settle(task)).outputs == {"amount": "1"}


async def test_approval_is_single_use_and_bound_to_action_and_generation(hub, api):
    task = asyncio.create_task(hub.escalate(req(hub), action_hash="h" * 64))
    await asyncio.sleep(0.05)
    await act(api, "i1", "claim")
    assert (await act(api, "i1", "approve", action_hash="x" * 64)).status_code == 409
    assert (await act(api, "i1", "approve", action_hash="h" * 64)).status_code == 200
    out = await settle(task)
    gen = out.lease.generation
    assert not hub.approvals.consume(out.approval_id, action_hash="other", generation=gen).ok
    assert not hub.approvals.consume(out.approval_id, action_hash="h" * 64, generation=gen).ok  # spent by the mismatch


async def test_verdict_only_for_commit_unknown(hub, api):
    task = asyncio.create_task(hub.escalate(req(hub, reason=COMMIT_UNKNOWN)))
    await asyncio.sleep(0.05)
    await act(api, "i1", "claim")
    assert (await act(api, "i1", "verdict", verdict="maybe")).status_code == 400
    assert (await act(api, "i1", "verdict", verdict="committed")).json()["verdict"] == "committed"
    assert (await settle(task)).kind == "verdict"
    assert (hub.writer.run_dir / "verdicts" / "i1.json").is_file()


async def test_foreign_host_and_origin_are_refused(api):
    assert (await api.get("/api/interventions", headers={"host": "evil.example"})).status_code == 403
    r = await api.post("/api/interventions/i1/abort", json={"operator": "a"}, headers={"origin": "http://evil.example"})
    assert r.status_code == 403


async def test_review_hub_serves_a_closed_bundle_and_records_a_verdict(hub, writer):
    hub.register(req(hub, reason=COMMIT_UNKNOWN))
    review = InterventionHub.review(writer.run_dir)
    transport = httpx.ASGITransport(app=build_app(review))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as c:
        assert (await c.get("/")).status_code == 200
        assert (await c.post("/api/interventions/i1/resume", json={"operator": "ann"})).status_code == 409
        r = await c.post("/api/interventions/i1/verdict", json={"operator": "ann", "verdict": "not_committed"})
        assert r.status_code == 200


def test_console_command_is_registered():
    p = argparse.ArgumentParser()
    register(p.add_subparsers())
    assert p.parse_args(["console", "x"]).func.__name__ == "cmd_console"


async def test_live_console_serves_a_session_hub(tmp_path, writer):
    from types import SimpleNamespace

    from cua.control.commands import live_console

    session = SimpleNamespace(evidence=writer, lease_manager=LeaseManager(), profile=None)
    async with live_console(session, notify=False) as hub:
        assert hub.live and hub.console_url.startswith("http://127.0.0.1:")
        async with httpx.AsyncClient(base_url=hub.console_url) as c:
            assert (await c.get("/api/interventions")).status_code == 200
