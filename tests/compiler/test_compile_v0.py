"""Compiler v0 on a hand-built trace: validate, keep, lift tokens, strategies, states, identity, determinism."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from trace_builder import build_trace, recorded, screen
from trace_builder import HOME as HOME_PAGE

from cua.agent.actions import Click, Navigate, RequestHelp, Type
from cua.artifact.lock import dumps, load_profile, lock_hash
from cua.artifact.schema import Capability
from cua.artifact.validate import validate_capability
from cua.compiler.compile import CompileError, compile_file, compile_to_json, compile_trace, lift
from cua.compiler.trace import RecordedTrace

PROFILE_PATH = Path(__file__).resolve().parents[2] / "apps" / "dolibarr" / "profile.json"
PROFILE = load_profile(PROFILE_PATH)
KW = dict(name="lookup_customer_outstanding", discovered_by="alice")


def compiled() -> Capability:
    return compile_trace(build_trace(), PROFILE, **KW)


def test_compiles_the_expected_program():
    cap = compiled()
    assert cap.id == "dolibarr.lookup_customer_outstanding" and cap.version == "1.0.0" and cap.app == "dolibarr"
    assert [(s.id, s.do.kind, s.expect) for s in cap.steps] == [
        ("s01_navigate", "navigate", "home"),
        ("s02_type", "type", "customer_list"),
        ("s03_press", "press", "customer_list"),
        ("s04_click", "click", "customer_list"),
        ("s05_assert", "assert", "customer_card"),
        ("s06_extract", "extract", "customer_card"),
    ]
    assert cap.effect == "read"
    assert [s.effect for s in cap.steps] == ["read", "none", "read", "read", "none", "read"]
    assert cap.success.state == "customer_card" and cap.success.outputs == ["outstanding_amount"]
    assert list(cap.contract.inputs) == ["customer_code"] and list(cap.contract.outputs) == ["outstanding_amount"]
    assert validate_capability(cap, PROFILE) == []


def test_tokens_are_lifted_in_actions_and_finders():
    cap = compiled()
    typed = cap.steps[1].do
    assert typed.text == "${customer_code}"
    link = cap.targets[cap.steps[3].do.target]
    assert any(s.row_key == "${customer_code}" for s in link.strategies)
    blob = dumps(cap)
    assert "{{" not in blob
    assert lift("/x?q=%7B%7Bcustomer_code%7D%7D&r={{customer_code}}") == "/x?q=${customer_code}&r=${customer_code}"


def test_navigation_becomes_an_origin_relative_route():
    cap = compiled()
    assert cap.steps[0].do.route == "/societe/list.php?leftmenu=customers&type=c"
    assert cap.steps[0].navigates and cap.steps[2].navigates and cap.steps[3].navigates
    assert not cap.steps[1].navigates


def test_strategies_are_ranked_and_deduplicated():
    cap = compiled()
    amount = cap.targets[cap.steps[5].do.target]
    assert [s.kind for s in amount.strategies] == ["role", "same_row"]  # rank 1 before rank 2
    link = cap.targets[cap.steps[3].do.target]
    assert [s.kind for s in link.strategies] == ["table_cell", "css"]  # rank 3 before rank 6 despite record order
    assert amount.fingerprint.row_label == "Current outstanding bill"


def test_value_echo_and_identity_assert_are_derived():
    cap = compiled()
    typed = cap.steps[1]
    assert typed.verify.kind == "target_equals_input" and typed.verify.input == "customer_code"
    assert typed.verify.normalizer == "exact" and typed.verify.target == typed.do.target
    ident = cap.steps[4]
    assert ident.do.kind == "assert" and ident.do.on_fail == "IDENTITY_MISMATCH"
    assert ident.do.that.input == "customer_code" and ident.do.that.normalizer == "text"
    shown = cap.targets[ident.do.that.target]
    assert shown.strategies[0].kind == "same_row" and shown.strategies[0].label == "Customer Code"


def test_done_evidence_becomes_the_success_checkpoint():
    cap = compiled()
    assert [c.kind for c in cap.success.checks] == ["target_visible"]
    assert cap.success.checks[0].target == cap.steps[4].do.that.target  # same element, one target


def test_provenance_binds_the_trace_but_the_body_has_no_status_or_hash():
    cap = compiled()
    assert cap.provenance.trace_run_id == "run-0001" and len(cap.provenance.trace_sha256) == 64
    assert cap.provenance.discovered_by == "alice" and cap.provenance.compiler == "cua.compiler/v0"
    text = dumps(cap)
    for word in ("approved", "status", "lock_hash"):
        assert word not in text


def test_same_trace_gives_byte_identical_json():
    first = compile_to_json(build_trace(), PROFILE, **KW)
    second = compile_to_json(build_trace(), PROFILE, **KW)
    assert first == second and "\r" not in first and first.endswith("}\n")
    # through the persisted form too: the compiler works from the file alone
    reloaded = RecordedTrace.model_validate_json(build_trace().model_dump_json(by_alias=True))
    assert compile_to_json(reloaded, PROFILE, **KW) == first
    assert lock_hash(Capability.model_validate(json.loads(first)), PROFILE) == lock_hash(compiled(), PROFILE)


def test_compile_file(tmp_path: Path):
    trace_path = tmp_path / "trace.json"
    trace_path.write_text(build_trace().model_dump_json(by_alias=True), encoding="utf-8")
    text = compile_file(trace_path, PROFILE_PATH, **KW)
    assert text == compile_to_json(build_trace(), PROFILE, **KW)


def test_changing_the_trace_changes_the_provenance_hash():
    other = build_trace(run_id="run-0002")
    assert compiled().provenance.trace_sha256 != compile_trace(other, PROFILE, **KW).provenance.trace_sha256


def test_blocked_and_errored_actions_are_dropped():
    trace = build_trace()
    extra = [
        recorded(7, Click(rationale="blocked by policy", ref="e1"), screen(HOME_PAGE, 5), status="blocked"),
        recorded(8, Click(rationale="stale ref", ref="e2"), screen(HOME_PAGE, 5), status="error", error="stale"),
    ]
    actions = trace.actions[:-1] + extra + trace.actions[-1:]
    # keep sequence numbers increasing
    actions = [a.model_copy(update={"seq": i}) for i, a in enumerate(actions, start=1)]
    cap = compile_trace(build_trace(actions=actions), PROFILE, **KW)
    assert [s.id for s in cap.steps] == [s.id for s in compiled().steps]


def test_unfinished_runs_are_refused():
    with pytest.raises(CompileError) as e:
        compile_trace(build_trace(outcome="stopped"), PROFILE, **KW)
    assert any("did not finish" in p for p in e.value.problems)


def test_run_must_end_with_done():
    trace = build_trace()
    with pytest.raises(CompileError) as e:
        compile_trace(build_trace(actions=trace.actions[:-1]), PROFILE, **KW)
    assert any("done" in p for p in e.value.problems)


def test_help_requests_are_refused():
    trace = build_trace()
    help_action = recorded(6, RequestHelp(rationale="captcha", reason="captcha"), screen(HOME_PAGE, 5))
    actions = trace.actions[:-1] + [help_action] + [trace.actions[-1].model_copy(update={"seq": 7})]
    with pytest.raises(CompileError) as e:
        compile_trace(build_trace(actions=actions), PROFILE, **KW)
    assert any("M5" in p for p in e.value.problems)


def test_outputs_must_be_extracted_once():
    with pytest.raises(CompileError) as e:
        compile_trace(build_trace(outputs={"outstanding_amount": {"type": "decimal"}, "other": {"type": "string"}}),
                      PROFILE, **KW)
    assert any("never extracted" in p for p in e.value.problems)
    trace = build_trace()
    twice = trace.actions[:-1] + [trace.actions[-2].model_copy(update={"seq": 6}),
                                  trace.actions[-1].model_copy(update={"seq": 7})]
    with pytest.raises(CompileError) as e:
        compile_trace(build_trace(actions=twice), PROFILE, **KW)
    assert any("2 times" in p for p in e.value.problems)


def test_undeclared_tokens_are_refused():
    trace = build_trace()
    bad = trace.actions[1].model_copy(update={"action": Type(rationale="x", ref="e5", text="{{other}}"),
                                              "tokens": ["other"]})
    with pytest.raises(CompileError) as e:
        compile_trace(build_trace(actions=[trace.actions[0], bad, *trace.actions[2:]]), PROFILE, **KW)
    assert any("undeclared input" in p for p in e.value.problems)


def test_screens_outside_the_profile_are_refused_with_near_misses():
    trace = build_trace()
    odd = screen(("/societe/list.php?x=1", "?", "- generic [ref=e1]: nothing useful\n"), 9)
    actions = [trace.actions[0].model_copy(update={"post": odd}),
               trace.actions[1].model_copy(update={"pre": odd}), *trace.actions[2:]]
    with pytest.raises(CompileError) as e:
        compile_trace(build_trace(actions=actions), PROFILE, **KW)
    assert any("unknown in the app profile" in p and "customer_list (1/2)" in p for p in e.value.problems)


def test_navigation_outside_the_origin_is_refused():
    trace = build_trace()
    off = trace.actions[0].model_copy(update={"action": Navigate(rationale="x", url="https://evil.example/a")})
    with pytest.raises(CompileError) as e:
        compile_trace(build_trace(actions=[off, *trace.actions[1:]]), PROFILE, **KW)
    assert any("outside the application origin" in p for p in e.value.problems)


def test_an_action_without_a_stable_locator_is_refused():
    trace = build_trace()
    bare = trace.actions[3].target.model_copy(update={"candidates": []})
    actions = list(trace.actions)
    actions[3] = actions[3].model_copy(update={"target": bare})
    with pytest.raises(CompileError) as e:
        compile_trace(build_trace(actions=actions), PROFILE, **KW)
    assert any("no stable locator" in p for p in e.value.problems)


def test_compiler_loads_no_browser_engine_model_sdk_or_replay():
    import subprocess
    import sys

    code = ("import sys, json; import cua.compiler.compile; "
            "print(json.dumps(sorted(m for m in sys.modules if m.split('.')[0] in "
            "{'playwright','anthropic','httpx','cua'})))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    loaded = set(json.loads(out))
    assert not {m for m in loaded if m.split(".")[0] in {"playwright", "anthropic", "httpx"}}
    assert not {m for m in loaded if m.startswith(("cua.replay", "cua.faults", "cua.compiler.recorder"))}


def test_a_wrong_app_is_refused():
    with pytest.raises(CompileError):
        compile_trace(build_trace(app="other"), PROFILE, **KW)

