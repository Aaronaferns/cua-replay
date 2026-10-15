"""A discovery trace with approved commit steps compiles to a commit capability that validates.

The fixture is the live Dolibarr 23 run (Pay opens the confirmation, Validate posts it) trimmed to its performed
actions, with a synthetic amount locator and a synthetic payment_ref extract appended."""

from __future__ import annotations

from pathlib import Path

import pytest

from cua.agent.actions import RequestApproval
from cua.artifact.lock import dumps, load_profile
from cua.artifact.predicates import AllOf, TargetEqualsInput, TargetVisible
from cua.artifact.schema import AssertAction, ClickAction, ExtractAction, Fingerprint, Strategy
from cua.artifact.validate import validate_capability
from cua.compiler.compile import CompileError, compile_trace
from cua.compiler.review_card import render_review_card
from cua.compiler.trace import LocatorCandidate, RecordedTrace, TargetRecord, TokenSighting
from cua.replay.effects import derived_effect

ROOT = Path(__file__).resolve().parents[2]
PROFILE = load_profile(ROOT / "apps" / "dolibarr" / "profile.json")
TRACE = RecordedTrace.model_validate_json((Path(__file__).parent / "fixtures" / "record_payment_trace.json").read_bytes())
KW = dict(name="record_invoice_payment", discovered_by="alice")


def compiled():
    return compile_trace(TRACE, PROFILE, **KW)


def test_commit_trace_compiles_and_validates():
    cap = compiled()
    assert validate_capability(cap, PROFILE) == []
    assert cap.id == "dolibarr.record_invoice_payment" and cap.effect == "commit"
    assert list(cap.contract.inputs) == ["invoice_ref", "amount", "method", "bank_account", "request_id"]
    assert list(cap.contract.outputs) == ["payment_ref"]
    assert {"validation_rejected", "invoice_not_found", "access_denied"} <= set(cap.contract.outcomes)
    assert "customer_not_found" not in cap.contract.outcomes
    assert compiled() == cap and dumps(compiled()) == dumps(cap)


def test_only_the_confirming_click_is_the_commit():
    cap = compiled()
    effects = {s.do.target: s.effect for s in cap.steps if isinstance(s.do, ClickAction)}
    assert effects["button_pay"] == "read" and effects["button_validate"] == "commit"
    assert [s.effect for s in cap.steps if s.effect == "commit"] == ["commit"]


def test_commit_step_has_review_asserts_for_the_inputs_and_a_verify():
    cap = compiled()
    commit = next(s for s in cap.steps if s.effect == "commit")
    review = [s.do.that for s in cap.steps if isinstance(s.do, AssertAction) and s.expect == commit.expect]
    assert sorted(p.input for p in review if isinstance(p, TargetEqualsInput)) == ["amount", "bank_account", "invoice_ref"]
    assert isinstance(commit.verify, TargetVisible | AllOf)
    review_steps = [s for s in cap.steps if s.expect == commit.expect and isinstance(s.do, AssertAction)]
    assert cap.steps.index(commit) > max(cap.steps.index(s) for s in review_steps)


def test_a_commit_with_no_confirmation_evidence_is_refused():
    trace = TRACE.model_copy(update={"actions": [a for a in TRACE.actions if a.action.kind != "extract"],
                                     "outputs": {}})
    with pytest.raises(CompileError) as e:
        compile_trace(trace, PROFILE, **KW)
    assert any("needs a verify" in p for p in e.value.problems)


def test_approval_requests_in_the_trace_are_not_a_reason_to_refuse():
    ask = TRACE.actions[8].model_copy(update={"action": RequestApproval(rationale="r", ref="f5e349", summary="s")})
    actions = [*TRACE.actions[:8], ask, *TRACE.actions[8:]]
    actions = [a.model_copy(update={"seq": i}) for i, a in enumerate(actions, start=1)]
    assert compile_trace(TRACE.model_copy(update={"actions": actions}), PROFILE, **KW).steps == compiled().steps


@pytest.mark.parametrize(("prompts", "declared", "expected"), [
    (True, "read", "read"), (False, "read", "commit"), (True, "commit", "commit")])
def test_a_prompting_click_is_not_raised_by_its_name(prompts, declared, expected):
    cap = compiled()
    step = next(s for s in cap.steps if isinstance(s.do, ClickAction) and s.do.target == "button_pay")
    step = step.model_copy(update={"effect": declared})
    got = derived_effect(PROFILE, step, url=None, target=cap.targets["button_pay"], dirty=True, prompts=prompts)
    assert got == expected


def test_review_card_renders_for_the_commit_capability():
    assert "commit" in render_review_card(compiled(), PROFILE).lower()


def _strategy(**fields) -> Strategy:
    return Strategy(why="t", matched_at_record="t", **fields)


def _payments_cell(role: str, column: str, row_key: str, name: str | None = None) -> TargetRecord:
    cands = [LocatorCandidate(rank=3, strategy=_strategy(kind="table_cell", column=column, row_key=row_key),
                              match_count=1, same_element=True)]
    if name:  # the new payment's ref, as the element's own name: it finds only the recorded payment
        cands.insert(0, LocatorCandidate(rank=1, strategy=_strategy(kind="role", role=role, name=name),
                                         match_count=1, same_element=True))
    return TargetRecord(ref="f6e1", role=role, name=name or "", candidates=cands,
                        fingerprint=Fingerprint(role=role, tag="td", column_header=column))


def test_a_finder_that_carries_the_new_record_is_left_out_when_a_structural_one_was_recorded():
    extract = TRACE.actions[9].model_copy(update={"target": _payments_cell("cell", "Payments", "{{method}}",
                                                                           name="PAY2610-0028")})
    cap = compile_trace(TRACE.model_copy(update={"actions": [*TRACE.actions[:9], extract, TRACE.actions[10]]}),
                        PROFILE, **KW)
    assert validate_capability(cap, PROFILE) == []
    tid = next(s.do.target for s in cap.steps if isinstance(s.do, ExtractAction))
    assert [(s.kind, s.column, s.row_key) for s in cap.targets[tid].strategies] == [("table_cell", "Payments",
                                                                                     "${method}")]


def test_the_amount_in_the_new_payment_row_is_the_commit_verify():
    commit = TRACE.actions[8].model_copy(update={"sightings": [
        TokenSighting(token="amount", target=_payments_cell("cell", "Amount", "{{method}}"))]})
    trace = TRACE.model_copy(update={"actions": [*TRACE.actions[:8], commit, TRACE.actions[10]], "outputs": {}})
    cap = compile_trace(trace, PROFILE, **KW)
    step = next(s for s in cap.steps if s.effect == "commit")
    assert isinstance(step.verify, TargetEqualsInput) and step.verify.input == "amount"
    assert cap.targets[step.verify.target].strategies[0].row_key == "${method}"
