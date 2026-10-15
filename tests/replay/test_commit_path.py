"""M6: the commit path (offline, scripted fakes). The oracle is the world's payment list: exactly one, or none."""

from __future__ import annotations

import asyncio
import json

import pytest
from fakes import BASE, AllowGuard, Clock, lease
from pay_world import INPUTS, PayWorld, capability, capability_dict, profile

from cua.artifact.validate import validate_capability
from cua.control.capture import CaptureEvent
from cua.control.console import ConsoleError, InterventionHub
from cua.control.lease import LeaseManager
from cua.evidence.writer import RunBundleWriter
from cua.replay.commit_ledger import CommitLedger
from cua.replay.interpreter import Credentials, replay

PROFILE = profile()


async def run(world, cap=None, inputs=None, **kw):
    clock = Clock()
    kw.setdefault("lease", lease)
    return await replay(cap or capability(), PROFILE, inputs or dict(INPUTS), surface=world, guard=world.guard,
                        base_url=BASE, credentials=Credentials("admin", "pw"), run_id="run-m6", now_ms=clock.now,
                        sleep=clock.sleep, **kw)


@pytest.fixture
def ledger():
    return CommitLedger()


def world(mode="ok"):
    return PayWorld(AllowGuard(), mode)


def test_the_payment_capability_validates():
    assert validate_capability(capability(), PROFILE) == []


async def test_confirmed_commit_is_a_success_with_one_payment(ledger):
    w = world()
    r = await run(w, ledger=ledger)
    assert r.status == "success" and r.outputs == {"payment_ref": "PAY-0001"}
    assert r.side_effects.commit_state == "confirmed" and r.side_effects.request_id == "req-1"
    assert w.payments == ["50.00"] and ledger.state("req-1") == "DONE"


async def test_overpayment_is_business_outcome(ledger):
    w = world("overpay")
    r = await run(w, ledger=ledger)
    assert r.status == "business_outcome" and r.outcome == "validation_rejected"
    assert r.side_effects.commit_state == "rejected" and r.retry_safe
    assert w.payments == [] and w.clicks == 1 and ledger.state("req-1") == "DONE"


async def test_unconfirmed_commit_is_indeterminate(ledger, tmp_path):
    w = world("lost")
    writer = RunBundleWriter("run-m6", root=tmp_path)
    r = await run(w, ledger=ledger, evidence=writer)
    writer.close(status="closed")
    assert r.status == "indeterminate" and r.outcome == "COMMIT_UNKNOWN" and not r.retry_safe
    assert r.failure.category == "commit" and r.side_effects.commit_state == "unknown"
    assert w.payments == ["50.00"] and w.clicks == 1 and ledger.state("req-1") == "UNKNOWN"
    kinds = [json.loads(x)["kind"] for x in (writer.run_dir / "events.jsonl").read_text("utf-8").splitlines()]
    assert kinds.index("commit_attempted") < len(kinds) - 1 - kinds[::-1].index("action")  # before the click's event


async def test_commit_clicked_once_on_lost_response(ledger):
    w = world("lost")
    first = await run(w, ledger=ledger)
    again = await run(w, ledger=ledger)  # the caller retries the same request
    assert first.status == again.status == "indeterminate" and again.run_id == first.run_id
    assert w.clicks == 1 and w.payments == ["50.00"]


async def test_unknown_commit_escalates_and_records_the_verdict(ledger, tmp_path):
    w = world("lost")
    writer = RunBundleWriter("run-m6", root=tmp_path)
    mgr = LeaseManager()
    hub = InterventionHub(run_id="run-m6", writer=writer, lease=mgr, poll_seconds=0.01)

    async def operator():
        while not hub.open_interventions():
            await asyncio.sleep(0.01)
        iid = hub.open_interventions()[0].id
        await mgr.wait_drained(5)
        await hub.claim(iid, "ann")
        await hub.verdict(iid, "ann", "committed")

    task = asyncio.create_task(operator())
    r = await asyncio.wait_for(run(w, ledger=ledger, hub=hub, evidence=writer, lease=mgr.token), 10)
    await task
    writer.close(status="closed")
    assert r.status == "indeterminate" and len(r.interventions) == 1 and w.payments == ["50.00"] and w.clicks == 1
    assert next((writer.run_dir / "verdicts").iterdir()).read_text("utf-8").count('"committed"') == 1


async def test_review_mismatch_blocks_commit(ledger):
    w = world()

    async def tamper(step):
        if step.id == "s05_commit":
            w.tamper = "5000.00"  # the page now shows another amount than the one requested

    r = await run(w, ledger=ledger, before_step=tamper)
    assert r.status == "failure" and r.failure.code == "REVIEW_MISMATCH" and r.retry_safe
    assert w.clicks == 0 and w.payments == [] and ledger.state("req-1") == "PREPARED"


async def test_draft_cannot_commit(ledger):
    w = world()
    r = await run(w, ledger=ledger, draft=True)
    assert r.failure.code == "DRAFT_CANNOT_COMMIT" and r.failure.category == "policy"
    assert w.log == [] and w.payments == []


@pytest.mark.parametrize("step_effect", ["read", "commit"])
async def test_effect_ceiling(step_effect):
    """A step the replay classifies above the capability's declared effect is blocked, whatever the step declares."""
    doc = capability_dict()
    doc["effect"] = "read"
    doc["steps"][4]["effect"] = step_effect  # a "Validate" click is a commit by its name even when declared read
    w = world()
    r = await run(w, capability_from(doc))
    assert r.failure.code == "EFFECT_EXCEEDS_CEILING" and r.failure.step_id == "s05_commit"
    assert w.clicks == 0 and w.payments == []


async def test_unnamed_submit_in_a_dirty_form_is_a_commit():
    doc = capability_dict()
    doc["effect"] = "read"
    doc["contract"]["inputs"]["note"] = {"type": "string"}
    doc["targets"]["note_box"] = {"description": "note", "fingerprint": {"role": "textbox", "tag": "input"},
                                  "strategies": [{"kind": "css", "css": "input[name='note']", "why": "t",
                                                  "matched_at_record": "1 of 1"}]}
    doc["targets"]["validate"] = {"description": "submit", "fingerprint": {"role": "button", "tag": "input",
                                                                         "input_type": "submit"},
                                  "strategies": [{"kind": "css", "css": "input[type=submit]", "why": "t",
                                                  "matched_at_record": "1 of 1"}]}
    doc["steps"].insert(4, {"id": "s04b_note", "intent": "Type a note", "expect": "payment_form", "effect": "none",
                            "do": {"kind": "type", "target": "note_box", "text": "${note}"}})
    w = world()
    r = await run(w, capability_from(doc), inputs={**INPUTS, "note": "hi"})
    assert r.failure.code == "EFFECT_EXCEEDS_CEILING" and w.clicks == 0


def capability_from(doc):
    from cua.artifact.schema import Capability

    return Capability.model_validate(doc)


async def test_same_request_id_returns_stored_result_with_no_ui_action(ledger):
    w = world()
    first = await run(w, ledger=ledger)
    seen = len(w.log)
    again = await run(w, ledger=ledger)
    assert again == first and len(w.log) == seen and w.payments == ["50.00"]


async def test_reused_request_id_with_other_inputs_is_rejected(ledger):
    w = world()
    await run(w, ledger=ledger)
    seen = len(w.log)
    r = await run(w, ledger=ledger, inputs={**INPUTS, "amount": "60.00"})
    assert r.failure.code == "REQUEST_ID_REUSED" and len(w.log) == seen and w.payments == ["50.00"]


def test_startup_sweep_marks_unknown(tmp_path):
    path = tmp_path / "ledger.sqlite"
    first = CommitLedger(path)
    first.begin("req-9", "h")
    assert first.attempt("req-9") and not first.attempt("req-9")  # the conditional update wins once
    first.close()
    assert CommitLedger(path).state("req-9") == "UNKNOWN"


async def test_relogin_never_reclicks_commit(ledger):
    w = world("expire")  # the session is gone when the response comes back
    r = await run(w, ledger=ledger)
    assert r.status == "indeterminate" and r.outcome == "COMMIT_UNKNOWN" and not r.retry_safe
    assert r.recoveries == [] and w.clicks == 1 and w.payments == ["50.00"]


async def test_resume_after_human_commit_never_commits(ledger, tmp_path):
    w = world()
    w.logged_in, w.captcha = False, True
    writer = RunBundleWriter("run-m6", root=tmp_path)
    mgr = LeaseManager()
    hub = InterventionHub(run_id="run-m6", writer=writer, lease=mgr, poll_seconds=0.01)
    refused = []

    async def human():
        while not hub.open_interventions():
            await asyncio.sleep(0.01)
        item = hub.open_interventions()[0]
        await mgr.wait_drained(5)
        await hub.claim(item.id, "ann")
        w.logged_in, w.captcha = True, False
        w.payments.append("50.00")  # the person validates the payment by hand
        w.url = f"{BASE}/compta/facture/card.php?facid=1&amount=50.00"
        item.handoff.note_human_action(CaptureEvent(kind="click", tag="input", type="submit", text="Validate"))
        with pytest.raises(ConsoleError) as e:
            await hub.resume(item.id, "ann")
        refused.append(e.value)
        await hub.mark_complete(item.id, "ann")

    task = asyncio.create_task(human())
    r = await asyncio.wait_for(run(w, ledger=ledger, hub=hub, evidence=writer, lease=mgr.token), 10)
    await task
    writer.close(status="closed")
    assert refused and r.status == "success" and w.clicks == 0 and w.payments == ["50.00"]
    assert ledger.state("req-1") == "DONE"
    again = await run(w, ledger=ledger)  # even the same request_id again must not pay twice
    assert again.status == "success" and w.clicks == 0
