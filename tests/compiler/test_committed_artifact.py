"""The committed artifact is exactly what the compiler makes of the committed discovery trace."""

from __future__ import annotations

from pathlib import Path

from cua.artifact.lock import load_capability, load_profile
from cua.artifact.validate import require_valid
from cua.compiler.compile import compile_file
from cua.compiler.trace import RecordedTrace

ROOT = Path(__file__).resolve().parents[2]
TRACE = ROOT / "evidence" / "discovery_lookup_customer_outstanding" / "trace.json"
PROFILE = ROOT / "apps" / "dolibarr" / "profile.json"
DRAFT = ROOT / "capabilities" / "dolibarr" / "lookup_customer_outstanding@1.0.0.draft.json"


def compile_committed_trace() -> str:
    trace = RecordedTrace.model_validate_json(TRACE.read_bytes())
    return compile_file(TRACE, PROFILE, name="lookup_customer_outstanding", discovered_by=trace.model)


def test_compile_from_trace_reproduces_draft():
    assert DRAFT.read_bytes() == compile_committed_trace().encode("utf-8")


def test_compile_twice_is_byte_identical():
    assert compile_committed_trace() == compile_committed_trace()


def test_committed_draft_is_valid_against_the_profile_and_traces_back_to_the_run():
    cap = load_capability(DRAFT)
    require_valid(cap, load_profile(PROFILE))
    assert cap.provenance.trace_run_id == "discovery_lookup_customer_outstanding"
    assert cap.provenance.discovered_by == "claude-opus-5-5"


FINAL = DRAFT.with_name("lookup_customer_outstanding@1.0.0.json")
SUITE = DRAFT.with_name("lookup_customer_outstanding@1.0.0.tests.json")
CARD = DRAFT.with_name("lookup_customer_outstanding@1.0.0.review.md")


def test_promoted_capability_is_the_validated_draft_unchanged():
    assert FINAL.read_bytes() == DRAFT.read_bytes()


def test_suite_covers_two_customers_and_fits_the_contract():
    from cua.artifact.suite import Suite, suite_problems

    suite = Suite.model_validate_json(SUITE.read_bytes())
    assert suite_problems(suite, load_capability(FINAL)) == []
    codes = {c.inputs["customer_code"] for c in suite.cases if c.expect.status == "success"}
    assert len(codes) >= 2
    assert all(c.expect.ground_truth for c in suite.cases if c.expect.status == "success")


def test_review_card_is_current():
    from cua.compiler.review_card import render_review_card

    text = render_review_card(load_capability(FINAL), load_profile(PROFILE))
    assert CARD.read_bytes() == text.encode("utf-8")


PAY_TRACE = ROOT / "evidence" / "discovery_record_payment" / "trace.json"
PAY_DRAFT = DRAFT.with_name("record_invoice_payment@1.0.0.draft.json")
# The approved payment draft is the compiler's output plus two reviewed edits (W7d, before approval), and nothing else:
#  1. s01 maps invoice_not_found to its outcome explicitly (the profile's default meaning already does the same);
#  2. the bank-account checks (s06, s12) read the dropdown, not the table cell that wraps it, whose text also holds
#     every hidden option.
REVIEWED_EDITS = [
    (("steps", 0, "on"), [{"state": "invoice_not_found", "outcome": "invoice_not_found"}]),
    (("steps", 5, "do", "that", "target"), "combobox_select_selectaccountid"),
    (("steps", 11, "do", "that", "target"), "combobox_select_selectaccountid"),
]
# Known issue in the approved 1.0.0 (REPORT §7): it was compiled before whole-number amounts with a currency counted
# as volatile, so button_validate keeps the recorded amount ("... for 50 Euros? ...") in one fallback finder and in its
# fingerprint's row label. Today's compiler leaves both out; 1.0.0 stays as approved until a re-approval. Nothing else
# may differ.
STALE_FINDERS = {"button_validate": 1}


def test_approved_payment_draft_is_the_compiled_trace_plus_exactly_the_reviewed_edits():
    import json

    from cua.artifact.validate import volatile_text

    compiled = json.loads(compile_file(PAY_TRACE, PROFILE, name="record_invoice_payment", discovered_by="aaron"))
    for path, value in REVIEWED_EDITS:
        *parents, last = path
        node = compiled
        for key in parents:
            node = node[key]
        assert node[last] != value, f"{path} is no longer an edit: the compiler produces it now"
        node[last] = value
    committed = json.loads(PAY_DRAFT.read_text(encoding="utf-8"))
    for target, count in STALE_FINDERS.items():
        strategies = committed["targets"][target]["strategies"]
        kept = [s for s in strategies
                if not any(volatile_text(v) for k, v in s.items() if k not in ("kind", "why", "matched_at_record") and v)]
        assert len(strategies) - len(kept) == count, f"{target}: the known stale finder is gone; drop this exception"
        committed["targets"][target]["strategies"] = kept
        fingerprint = committed["targets"][target]["fingerprint"]
        assert volatile_text(fingerprint.pop("row_label")), f"{target}: the known stale row label is gone"
    assert committed == compiled
