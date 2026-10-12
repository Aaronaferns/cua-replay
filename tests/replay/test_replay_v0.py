"""Replay v0 against the in-memory fake surface: the happy path and each way the screen can disagree."""

from __future__ import annotations

import json

import pytest
from fakes import BASE, AllowGuard, Clock, DenyGuard, FakeDolibarr, lease
from sample_docs import capability, capability_dict, dolibarr_profile

from cua.artifact.lock import lock_hash
from cua.artifact.schema import Capability
from cua.replay.interpreter import Credentials, Timing, replay
from cua.replay.result import RunResult

CODE = "CU1212-0005"
OTHER = "CU1910-00021"
PROFILE = dolibarr_profile()


async def run(fake: FakeDolibarr, cap: Capability | None = None, inputs: dict[str, str] | None = None, *,
              guard: AllowGuard | None = None, **kw) -> RunResult:
    clock = Clock()
    return await replay(cap or capability(), PROFILE, inputs if inputs is not None else {"customer_code": CODE},
                        surface=fake, guard=guard or fake.guard, lease=lease, base_url=BASE, run_id="run-test",
                        now_ms=clock.now, sleep=clock.sleep, **kw)


@pytest.fixture
def guard() -> AllowGuard:
    return AllowGuard()


@pytest.fixture
def fake(guard) -> FakeDolibarr:
    return FakeDolibarr(guard)


def assert_no_values(result: RunResult, *values: str) -> None:
    blob = result.model_dump_json()
    for v in values:
        assert v not in blob


# --- success ---------------------------------------------------------------------------------------------------


async def test_replay_succeeds_and_returns_the_typed_output(fake):
    r = await run(fake)
    assert r.status == "success" and r.outcome == "success" and r.failure is None
    assert r.outputs == {"outstanding_amount": "75.37"}
    assert r.drift == [] and r.recoveries == [] and r.retry_safe
    assert r.side_effects.commit_state == "none" and r.side_effects.by is None
    assert r.capability == "dolibarr.lookup_customer_outstanding" and r.version == "1.0.0"
    assert r.lock_hash == lock_hash(capability(), PROFILE)
    assert set(r.timings) >= {"total_ms", "s01_open_list", "s04_read_amount"}


async def test_replay_works_for_a_different_customer(guard):
    for code, amount in ((CODE, "75.37"), (OTHER, "1234.50")):
        r = await run(FakeDolibarr(guard), inputs={"customer_code": code})
        assert r.status == "success" and r.outputs == {"outstanding_amount": amount}


async def test_every_action_carries_the_lease_and_the_effect_and_extracts_are_guarded(fake, guard):
    r = await run(fake)
    assert r.status == "success"
    kinds = [(q.kind, q.actor, q.effect) for q in guard.requests]
    assert kinds == [("navigate", "replay", "none"), ("navigate", "replay", "read"), ("click", "replay", "read"),
                     ("extract", "replay", "read")]
    assert all(q.lease.holder == "automation" and q.lease.generation == 1 for q in guard.requests)
    assert guard.requests[1].target_url == f"{BASE}/societe/list.php?leftmenu=customers&type=c&search_code={CODE}"
    assert guard.requests[1].page_url.endswith("/index.php?mainmenu=home&leftmenu=home")


async def test_input_values_are_url_encoded_in_routes(guard):
    cap_doc = capability_dict()
    cap_doc["contract"]["inputs"]["customer_code"].pop("pattern")
    fake = FakeDolibarr(guard)
    r = await run(fake, Capability.model_validate(cap_doc), {"customer_code": "A B&c=d"})
    assert r.status == "failure"  # the fake has no such customer; what matters is the request that was made
    assert guard.requests[1].target_url.endswith("search_code=A%20B%26c%3Dd")


async def test_the_result_never_contains_the_input_value(fake):
    for broken in (False, True):
        fake.card_code_override = "CU9999-0001" if broken else None
        r = await run(fake)
        assert r.status == ("failure" if broken else "success")
        assert_no_values(r, CODE, "CU9999-0001")
        assert CODE not in json.dumps(r.failure.model_dump() if r.failure else {})


# --- inputs ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("inputs", [{}, {"customer_code": "bad"}, {"customer_code": CODE, "extra": "x"},
                                    {"customer_code": f" {CODE}"}])
async def test_invalid_inputs_fail_before_any_action(fake, guard, inputs):
    r = await run(fake, inputs=inputs)
    assert r.status == "failure" and r.failure.code == "INPUT_INVALID" and r.failure.category == "caller"
    assert guard.requests == [] and fake.log == [] and r.retry_safe
    assert_no_values(r, "bad")


# --- policy ----------------------------------------------------------------------------------------------------


async def test_policy_denial_is_a_failure_with_category_policy_and_ui_untouched():
    guard = DenyGuard({"click"}, approvable=True)
    fake = FakeDolibarr(guard)
    r = await run(fake)
    assert r.status == "failure" and r.outcome == "POLICY_DENIED"
    assert r.failure.category == "policy" and r.failure.approvable
    assert r.failure.step_id == "s02_open_card" and "click is not allowed" in r.failure.detail
    assert "click link" not in fake.log


async def test_a_denied_extract_returns_no_output():
    guard = DenyGuard({"extract"})
    r = await run(FakeDolibarr(guard))
    assert r.failure.category == "policy" and r.failure.step_id == "s04_read_amount" and r.outputs == {}


# --- states ----------------------------------------------------------------------------------------------------


async def test_logged_out_entry_logs_in_with_the_profile_and_records_a_recovery(guard):
    fake = FakeDolibarr(guard, logged_in=False)
    r = await run(fake, credentials=Credentials("admin", "pw"))
    assert r.status == "success" and r.outputs == {"outstanding_amount": "75.37"}
    assert [(x.handler, x.state) for x in r.recoveries] == [("login", "login")]
    assert [q.actor for q in guard.requests[:3]] == ["replay", "handler", "handler"]
    assert "pw" not in r.model_dump_json() and "pw" not in repr(Credentials("admin", "pw"))


async def test_logged_out_without_credentials_fails_closed(guard):
    r = await run(FakeDolibarr(guard, logged_in=False))
    assert r.status == "failure" and r.failure.code == "LOGIN_REQUIRED"
    assert r.failure.expected == "home"


async def test_wrong_password_is_login_failed_not_a_loop(guard):
    r = await run(FakeDolibarr(guard, logged_in=False, password="other"), credentials=Credentials("admin", "pw"))
    assert r.status == "failure" and r.failure.code == "LOGIN_FAILED"


async def test_login_form_at_an_app_url_mid_run_uses_the_profile_meaning(fake):
    fake.expire_on_card = True
    r = await run(fake)
    assert r.status == "failure" and r.outcome == "SESSION_EXPIRED" and r.failure.category == "state"
    assert r.failure.step_id == "s02_open_card" and r.failure.expected == "customer_card"
    assert r.failure.observed.state == "login"
    assert r.outputs == {}


async def test_unknown_screen_fails_with_expected_observed_and_near_misses(fake):
    fake.list_header = False
    r = await run(fake)
    assert r.status == "failure" and r.failure.code == "UNKNOWN_STATE"
    assert r.failure.expected == "customer_list" and r.failure.observed.state == "unknown"
    assert r.failure.observed.near_misses[0] == "customer_list (1/2)"
    assert r.failure.observed.url == "/societe/list.php?leftmenu=*&type=*&search_code=*"
    assert CODE not in r.model_dump_json()


async def test_unexpected_known_state_is_reported_with_its_name(fake):
    cap_doc = capability_dict()
    cap_doc["steps"][0]["do"]["route"] = "/index.php?mainmenu=home"  # stays on the home screen
    r = await run(fake, Capability.model_validate(cap_doc))
    assert r.failure.code == "UNEXPECTED_STATE" and r.failure.observed.state == "home"


async def test_ambiguous_state_is_failure(guard):
    from cua.artifact.predicates import Text
    from cua.artifact.schema import State

    states = dict(PROFILE.states)
    states["home_twin"] = State(description="twin", signature=Text(equals="Global view"), origin="declared")
    twin = PROFILE.model_copy(update={"states": states})
    clock = Clock()
    r = await replay(capability(), twin, {"customer_code": CODE}, surface=FakeDolibarr(guard), guard=guard,
                     lease=lease, base_url=BASE, now_ms=clock.now, sleep=clock.sleep)
    assert r.status == "failure" and r.failure.code == "AMBIGUOUS_STATE"
    assert "home" in r.failure.detail and "home_twin" in r.failure.detail


async def test_replay_waits_for_the_page_to_settle(fake):
    fake.delay_polls = 3  # the screen shows "Loading..." (unknown) for three observations after each load
    r = await run(fake)
    assert r.status == "success"


async def test_a_page_that_never_arrives_times_out_into_a_state_failure(fake):
    fake.delay_polls = 10_000
    r = await run(fake)
    assert r.status == "failure" and r.failure.code == "UNKNOWN_STATE" and r.failure.category == "state"


async def test_branch_to_a_declared_business_outcome(fake):
    doc = capability_dict()
    doc["contract"]["outcomes"] = {"customer_not_found": {"description": "No such customer"}}
    doc["steps"][0]["on"] = [{"state": "customer_list", "then": "next"},
                             {"state": "home", "outcome": "customer_not_found"}]
    doc["steps"][0]["do"]["route"] = "/index.php?mainmenu=home"
    r = await run(fake, Capability.model_validate(doc))
    assert r.status == "business_outcome" and r.outcome == "customer_not_found"
    assert r.failure is None and r.outputs == {} and r.retry_safe


async def test_native_dialog_is_never_accepted(fake):
    fake.native_on_card = True
    r = await run(fake)
    assert r.failure.code == "NATIVE_DIALOG_OPEN" and r.failure.category == "state"


# --- identity and checks ---------------------------------------------------------------------------------------


async def test_identity_mismatch_stops_before_any_value_is_read(fake, guard):
    fake.card_code_override = "CU9999-0001"
    r = await run(fake)
    assert r.status == "failure" and r.failure.code == "IDENTITY_MISMATCH" and r.failure.category == "data"
    assert r.failure.step_id == "s03_identity" and r.outputs == {}
    assert all(q.kind != "extract" for q in guard.requests)
    assert_no_values(r, CODE, "CU9999-0001")


async def test_missing_identity_evidence_is_not_a_pass(fake):
    fake.card_has_value_cell = False
    r = await run(fake)
    assert r.status == "failure" and r.failure.code == "TARGET_NOT_FOUND" and r.failure.step_id == "s03_identity"


async def test_value_echo_verify_failure(fake):
    doc = capability_dict()
    doc["steps"][3]["verify"] = {"kind": "text", "equals": "never shown"}
    r = await run(fake, Capability.model_validate(doc))
    assert r.failure.code == "CHECKPOINT_FAILED" and r.failure.step_id == "s04_read_amount"


async def test_success_checks_must_hold_on_the_final_screen(fake):
    doc = capability_dict()
    doc["success"]["checks"] = [{"kind": "text", "equals": "Paid in full"}]
    r = await run(fake, Capability.model_validate(doc))
    assert r.failure.code == "CHECKPOINT_FAILED" and r.failure.expected == "customer_card" and r.outputs == {}


async def test_unparseable_output_is_a_data_failure_without_the_text(fake):
    fake.amount_text_override = "n/a (secret text)"
    r = await run(fake)
    assert r.failure.code == "OUTPUT_UNPARSEABLE" and r.failure.category == "data"
    assert "secret" not in r.model_dump_json()


# --- resolution ------------------------------------------------------------------------------------------------


async def test_fallback_strategy_is_used_and_reported_as_drift(fake):
    fake.row_key_visible = False  # the primary table_cell strategy no longer matches
    r = await run(fake)
    assert r.status == "success"
    assert len(r.drift) == 1
    d = r.drift[0]
    assert (d.step_id, d.target) == ("s02_open_card", "customer_link")
    assert d.failed_strategies == ['link in column "Third-party name" of the row keyed "${customer_code}" [no match]']
    assert d.used_strategy == 'control in the row labelled "Customer Code"'
    assert CODE not in r.model_dump_json()


async def test_fingerprint_blocks_misbound_fallback(fake):
    fake.row_key_visible = False
    fake.link_tag = "span"  # the fallback finds exactly one element, but it is not what was recorded
    r = await run(fake)
    assert r.status == "failure" and r.failure.code == "TARGET_FINGERPRINT_MISMATCH" and r.failure.category == "locator"
    assert "tag" in r.failure.detail
    assert "click link" not in fake.log


async def test_many_matches_is_failure_never_the_first(fake):
    fake.duplicate_rows = True
    r = await run(fake)
    assert r.status == "failure" and r.failure.code == "TARGET_AMBIGUOUS"
    assert "2 matches" in r.failure.detail and "click link" not in fake.log


async def test_zero_matches_is_failure(fake):
    fake.row_key_visible = False
    fake.link_tag = "a"
    doc = capability_dict()
    doc["targets"]["customer_link"]["strategies"].pop()  # no fallback left
    r = await run(fake, Capability.model_validate(doc))
    assert r.failure.code == "TARGET_NOT_FOUND" and r.failure.step_id == "s02_open_card"


async def test_extract_is_primary_only(fake):
    fake.amount_has_row_label = False  # only the role fallback would still find the amount
    r = await run(fake)
    assert r.status == "failure" and r.failure.code == "TARGET_NOT_FOUND" and r.failure.step_id == "s04_read_amount"
    assert r.drift == [] and r.outputs == {}
    assert "primary strategy" in r.failure.detail


async def test_resolution_uses_the_substituted_input_but_reports_templates(fake):
    fake.row_key_visible = False
    r = await run(fake)
    assert "${customer_code}" in r.drift[0].failed_strategies[0]


# --- commit bookkeeping (no commit flow yet, but the result must not lie) --------------------------------------


async def test_commit_effect_marks_the_run_not_retry_safe(guard):
    doc = capability_dict()
    doc["effect"] = "commit"
    doc["steps"][1]["effect"] = "commit"
    r = await run(FakeDolibarr(guard), Capability.model_validate(doc))
    assert r.status == "success"
    assert r.side_effects.commit_state == "attempted" and r.side_effects.by == "automation" and not r.retry_safe
    assert [q.effect for q in guard.requests if q.kind == "click"] == ["commit"]


async def test_surface_error_during_a_commit_action_is_indeterminate(guard):
    from cua.surface.base import SurfaceError

    class Flaky(FakeDolibarr):
        async def click(self, element, request):
            await super().click(element, request)
            raise SurfaceError("connection lost")

    doc = capability_dict()
    doc["effect"] = "commit"
    doc["steps"][1]["effect"] = "commit"
    r = await run(Flaky(guard), Capability.model_validate(doc))
    assert r.status == "indeterminate" and r.failure.code == "COMMIT_UNKNOWN" and r.failure.category == "commit"
    assert r.side_effects.commit_state == "unknown" and not r.retry_safe


async def test_surface_error_on_a_read_is_a_transient_failure(guard):
    from cua.surface.base import SurfaceError

    class Flaky(FakeDolibarr):
        async def click(self, element, request):
            raise SurfaceError("detached")

    r = await run(Flaky(guard))
    assert r.status == "failure" and r.failure.code == "SURFACE_ERROR" and r.failure.category == "transient"
    assert r.retry_safe


async def test_timing_can_be_tuned(fake):
    r = await run(fake, timing=Timing(poll_interval_ms=10, stable_polls=1))
    assert r.status == "success"
