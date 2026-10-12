"""M4: every runtime condition lands in a deliberate status (offline, against the in-memory fake)."""

from __future__ import annotations

import pytest
from fakes import BASE, AllowGuard, Clock, FakeDolibarr, lease
from sample_docs import capability, capability_dict, dolibarr_profile

from cua.artifact.schema import Capability
from cua.replay.interpreter import Credentials, replay

CODE = "CU1212-0005"
PROFILE = dolibarr_profile()
CREDS = Credentials("admin", "pw")
PNG = bytes.fromhex("89504e470d0a1a0a")


@pytest.fixture
def guard() -> AllowGuard:
    return AllowGuard()


@pytest.fixture
def fake(guard) -> FakeDolibarr:
    return FakeDolibarr(guard)


async def run(fake, cap=None, *, code=CODE, **kw):
    clock = Clock()
    return await replay(cap or capability(), PROFILE, {"customer_code": code}, surface=fake, guard=fake.guard,
                        lease=lease, base_url=BASE, credentials=CREDS, run_id="run-m4", now_ms=clock.now,
                        sleep=clock.sleep, **kw)


def expire_once(fake, step_id):
    done = []

    async def before_step(step):
        if step.id == step_id and not done:
            done.append(1)
            fake.expire_cards = 1

    return before_step


def with_outcomes(*names):
    doc = capability_dict()
    doc["contract"]["outcomes"] = {n: {"description": n} for n in names}
    return Capability.model_validate(doc)


# --- session expiry ---------------------------------------------------------------------------------------------


async def test_session_expiry_relogs_in_and_restarts_when_retry_safe(fake):
    r = await run(fake, before_step=expire_once(fake, "s02_open_card"))
    assert r.status == "success" and r.outputs == {"outstanding_amount": "75.37"}
    assert [(x.handler, x.state, x.step_id) for x in r.recoveries] == [("login", "login", "s02_open_card")]


async def test_session_expiring_again_after_the_restart_is_session_expired(fake):
    fake.expire_on_card = True
    r = await run(fake)
    assert r.status == "failure" and r.failure.code == "SESSION_EXPIRED" and r.failure.category == "state"
    assert [x.handler for x in r.recoveries] == ["login"]


async def test_session_expiry_after_a_commit_is_never_restarted(fake):
    doc = capability_dict()
    doc["effect"] = "commit"
    doc["steps"][0]["effect"] = "commit"
    r = await run(fake, Capability.model_validate(doc), before_step=expire_once(fake, "s02_open_card"))
    assert r.status == "failure" and r.failure.code == "SESSION_EXPIRED" and not r.retry_safe
    assert r.recoveries == []


async def test_captcha_login_is_human_only_and_never_filled(fake, guard):
    fake.logged_in, fake.captcha = False, True
    r = await run(fake)
    assert r.status == "failure" and r.failure.code == "HUMAN_ONLY_LOGIN" and r.failure.category == "control"
    assert r.failure.observed.state == "login_captcha" and r.recoveries == []
    assert not any(q.actor == "handler" for q in guard.requests)


# --- dialogs ----------------------------------------------------------------------------------------------------


async def test_known_interstitial_is_dismissed_and_recorded(fake, guard):
    fake.notice = "Service notice"
    r = await run(fake)
    assert r.status == "success" and r.outputs == {"outstanding_amount": "75.37"}
    assert [(x.handler, x.state) for x in r.recoveries] == [("dismiss_notice", "service_notice")]
    assert len([q for q in guard.requests if q.actor == "handler"]) == 1


async def test_unknown_dialog_fails_closed(fake, guard):
    fake.notice, fake.notice_buttons = "Quick survey", ["Maybe later", "Take the survey"]
    r = await run(fake)
    assert r.status == "failure" and r.failure.code == "UNKNOWN_DIALOG" and r.failure.category == "state"
    assert r.failure.observed.state == "unknown" and r.recoveries == []
    assert fake.notice is not None  # nothing in the dialog was clicked
    assert not any(q.actor == "handler" for q in guard.requests)


# --- data outcomes and errors -----------------------------------------------------------------------------------


async def test_unknown_code_is_customer_not_found(fake):
    r = await run(fake, with_outcomes("customer_not_found"), code="CU0000-0000")
    assert r.status == "business_outcome" and r.outcome == "customer_not_found" and r.outputs == {}


async def test_access_denied_is_a_business_outcome(fake):
    fake.access_denied_on_list = True
    r = await run(fake, with_outcomes("access_denied"))
    assert r.status == "business_outcome" and r.outcome == "access_denied" and r.outputs == {}


async def test_near_miss_is_identity_mismatch(fake, guard):
    """The card shown is a different customer: the identity assert stops the run before any amount is read."""
    fake.card_code_override = "CU1212-00051"
    r = await run(fake)
    assert r.status == "failure" and r.failure.code == "IDENTITY_MISMATCH" and r.outputs == {}
    assert r.failure.category == "data" and r.failure.observed.state == "customer_card"
    assert all(q.kind != "extract" for q in guard.requests)


async def test_app_error_page_is_an_app_failure(fake):
    fake.app_error_on_card = True
    r = await run(fake)
    assert r.status == "failure" and r.failure.code == "APP_ERROR" and r.failure.category == "app"
    assert r.retry_safe and r.failure.observed.state == "app_error"


# --- slowness ---------------------------------------------------------------------------------------------------


async def test_slow_within_timeout_not_failure(fake):
    fake.delay_polls = 4
    r = await run(fake)
    assert r.status == "success" and r.failure is None


async def test_slow_beyond_timeout_transient_exhausted(fake):
    doc = capability_dict()
    doc["steps"][1]["timeout_ms"] = 500
    fake.hang_on_click = True
    r = await run(fake, Capability.model_validate(doc))
    assert r.status == "failure" and r.failure.code == "TRANSIENT_EXHAUSTED" and r.failure.category == "transient"
    assert r.failure.step_id == "s02_open_card" and r.retry_safe
    assert fake.log.count("click link") == 2  # one retry, then give up


# --- the result -------------------------------------------------------------------------------------------------


async def test_failure_carries_expected_observed_and_masked_url(fake):
    fake.app_error_on_card = True
    r = await run(fake)
    f = r.failure
    assert f.step_id == "s02_open_card" and f.step_intent and f.expected == "customer_card"
    assert f.observed.url.endswith("?code=*") and CODE not in r.model_dump_json()


async def test_failure_evidence_is_captured_through_the_evidence_writer(fake, tmp_path):
    from cua.evidence.writer import RunBundleWriter

    fake.app_error_on_card = True

    async def shot(masks, *, generic):
        assert generic
        return PNG

    fake.masked_screenshot = shot
    writer = RunBundleWriter("run-m4-ev", root=tmp_path)
    r = await run(fake, evidence=writer)
    assert r.failure.evidence == ["failure/s02_open_card.png", "failure/s02_open_card.snapshot.json"]


def test_replay_never_imports_faults():
    import subprocess
    import sys

    code = ("import sys, cua.replay.interpreter, cua.replay.suite, cua.replay.result\n"
            "print(sorted(m for m in sys.modules if m.startswith('cua.faults')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"
