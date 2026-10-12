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
