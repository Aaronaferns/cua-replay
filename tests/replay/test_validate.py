"""`cua validate` and the suite comparison, offline: fake session, fake reset, fake oracle."""

from __future__ import annotations

import json

import pytest
from sample_docs import capability, capability_dict
from test_cli import caps, sessions, write  # noqa: F401  (fixtures)

import cua.replay.commands as commands
from cua.artifact.lock import lock_hash
from cua.artifact.suite import Suite, suite_problems
from cua.cli import main
from cua.replay.result import RunResult
from cua.replay.suite import compare

NAME = "lookup_customer_outstanding@1.0.0"
OK, OTHER = "CU1212-0005", "CU1910-00021"


def suite_doc(**over):
    doc = {
        "schema": "cua.suite/v1", "capability": "dolibarr.lookup_customer_outstanding",
        "cases": [
            {"id": "first", "inputs": {"customer_code": OK},
             "expect": {"status": "success", "outcome": "success", "outputs": {"outstanding_amount": "75.37"}}},
            {"id": "second", "inputs": {"customer_code": OTHER},
             "expect": {"status": "success", "outcome": "success",
                        "ground_truth": [{"output": "outstanding_amount", "oracle": "dolibarr.customer_outstanding",
                                          "input": "customer_code"}]}},
            {"id": "bad_code", "inputs": {"customer_code": "not-a-code"}, "reset": False,
             "expect": {"status": "failure", "outcome": "INPUT_INVALID"}},
        ],
    }
    return doc | over


@pytest.fixture
def harness(caps, sessions, monkeypatch, tmp_path):
    calls = {"reset": 0, "oracle": []}
    monkeypatch.setattr(commands, "_reset_target", lambda: calls.__setitem__("reset", calls["reset"] + 1))

    def oracle(name, value):
        calls["oracle"].append((name, value))
        return "1234.50"

    monkeypatch.setattr(commands, "_oracle", oracle)
    write(caps, f"{NAME}.json")
    return calls


def put_suite(caps, doc):
    (caps / "lookup_customer_outstanding.tests.json").write_text(json.dumps(doc), encoding="utf-8")


def summary_of(run_id):
    return json.loads((commands.validation_dir() / run_id / "summary.json").read_text(encoding="utf-8"))


def test_validate_runs_each_case_resets_per_case_and_writes_the_summary(caps, harness, capsys):
    put_suite(caps, suite_doc())
    code = main(["validate", "dolibarr.lookup_customer_outstanding", "--run-id", "val-t1"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert harness["reset"] == 2  # the third case opted out
    assert harness["oracle"] == [("dolibarr.customer_outstanding", OTHER)]
    s = summary_of("val-t1")
    assert s["passed"] is True and s["repeat"] == 1 and s["version"] == "1.0.0"
    assert s["lock_hash"] == lock_hash(capability(), commands.load_profile(commands.profile_path_of("dolibarr")))
    assert [(c["case_id"], c["passed"], c["status"]) for c in s["cases"]] == [
        ("first", True, "success"), ("second", True, "success"), ("bad_code", True, "failure")]
    assert OK not in json.dumps(s) and "3/3 passed" in out


def test_validate_repeat_runs_the_whole_suite_n_times(caps, harness):
    put_suite(caps, suite_doc())
    assert main(["validate", "dolibarr.lookup_customer_outstanding", "--repeat", "2", "--run-id", "val-t2"]) == 0
    s = summary_of("val-t2")
    assert s["repeat"] == 2 and [c["repeat"] for c in s["cases"]] == [1, 1, 1, 2, 2, 2]
    assert len({c["run_id"] for c in s["cases"]}) == 6


def test_repeat_writes_the_suite_report_and_names_flakes(caps, harness, monkeypatch):
    put_suite(caps, suite_doc())
    seen = iter(["1234.50", "1.00", "1234.50"])  # the oracle disagrees with itself once: case "second" flakes
    monkeypatch.setattr(commands, "_oracle", lambda name, value: next(seen))
    assert main(["validate", "dolibarr.lookup_customer_outstanding", "--repeat", "3", "--run-id", "val-t6"]) == 1
    report = json.loads((commands.evidence_dir() / "validation_suite.json").read_text(encoding="utf-8"))
    assert report["n"] == 3 and report["flakes"] == ["second"]
    assert {c["id"]: c["passed"] for c in report["cases"]} == {"first": 3, "second": 2, "bad_code": 3}


def test_a_single_run_does_not_write_the_suite_report(caps, harness):
    put_suite(caps, suite_doc())
    assert main(["validate", "dolibarr.lookup_customer_outstanding", "--run-id", "val-t7"]) == 0
    assert not (commands.evidence_dir() / "validation_suite.json").exists()


def test_validate_gives_replay_no_hub(caps, harness, monkeypatch):
    put_suite(caps, suite_doc())
    hubs = []

    async def spy(*args, **kw):
        hubs.append(kw["hub"])
        return RunResult(run_id="r", capability="dolibarr.lookup_customer_outstanding", version="1.0.0",
                         lock_hash="x" * 64, status="failure", outcome="INPUT_INVALID", retry_safe=True)

    monkeypatch.setattr(commands, "replay", spy)
    main(["validate", "dolibarr.lookup_customer_outstanding", "--run-id", "val-t8"])
    assert hubs and all(h is None for h in hubs)


def test_a_mismatch_fails_the_run_and_names_it_without_values(caps, harness, capsys):
    doc = suite_doc()
    doc["cases"][0]["expect"]["outputs"]["outstanding_amount"] = "99.99"
    put_suite(caps, doc)
    assert main(["validate", "dolibarr.lookup_customer_outstanding", "--run-id", "val-t3"]) == 1
    s = summary_of("val-t3")
    assert s["passed"] is False and s["cases"][0]["passed"] is False
    assert any("outstanding_amount" in m for m in s["cases"][0]["mismatches"])
    assert "FAIL" in capsys.readouterr().out


def test_ground_truth_disagreement_fails_the_case(caps, harness, monkeypatch):
    put_suite(caps, suite_doc())
    monkeypatch.setattr(commands, "_oracle", lambda name, value: "1.00")
    assert main(["validate", "dolibarr.lookup_customer_outstanding", "--run-id", "val-t4"]) == 1
    assert summary_of("val-t4")["cases"][1]["passed"] is False


def test_a_case_that_cannot_run_is_a_failed_case_not_a_crash(caps, harness, monkeypatch):
    put_suite(caps, suite_doc())

    def boom():
        raise RuntimeError("target is down")

    monkeypatch.setattr(commands, "_reset_target", boom)
    assert main(["validate", "dolibarr.lookup_customer_outstanding", "--run-id", "val-t5"]) == 1
    cases = summary_of("val-t5")["cases"]
    assert cases[0]["status"] is None and "RuntimeError" in cases[0]["mismatches"][0]
    assert "target is down" not in json.dumps(cases)


def test_a_suite_that_disagrees_with_the_contract_is_a_usage_error(caps, harness, capsys):
    doc = suite_doc()
    doc["cases"][0]["inputs"] = {"nope": "x"}
    put_suite(caps, doc)
    assert main(["validate", "dolibarr.lookup_customer_outstanding"]) == 2
    assert "unknown input" in capsys.readouterr().err
    assert harness["reset"] == 0


def test_missing_suite_and_bad_repeat_are_usage_errors(caps, harness, capsys):
    assert main(["validate", "dolibarr.lookup_customer_outstanding"]) == 2
    assert "no suite" in capsys.readouterr().err
    put_suite(caps, suite_doc())
    assert main(["validate", "dolibarr.lookup_customer_outstanding", "--repeat", "0"]) == 2


def test_a_versioned_suite_wins_over_the_plain_one(caps, harness):
    put_suite(caps, suite_doc())
    (caps / f"{NAME}.tests.json").write_text(json.dumps(suite_doc(cases=suite_doc()["cases"][:1])), encoding="utf-8")
    assert main(["validate", "dolibarr.lookup_customer_outstanding", "--run-id", "val-t6"]) == 0
    assert len(summary_of("val-t6")["cases"]) == 1


# --- schema and comparison ---------------------------------------------------------------------------------------


def test_suite_schema_is_strict_and_unique():
    with pytest.raises(ValueError):
        Suite.model_validate(suite_doc(extra=1))
    doc = suite_doc()
    doc["cases"][1]["id"] = "first"
    with pytest.raises(ValueError):
        Suite.model_validate(doc)
    doc = suite_doc()
    doc["cases"][0]["expect"]["status"] = "maybe"
    with pytest.raises(ValueError):
        Suite.model_validate(doc)
    doc = suite_doc()
    doc["cases"][1]["expect"]["ground_truth"][0]["oracle"] = "anything.else"
    with pytest.raises(ValueError):
        Suite.model_validate(doc)


def test_suite_problems_check_references_by_name():
    cap = capability()
    assert suite_problems(Suite.model_validate(suite_doc()), cap) == []
    doc = suite_doc(capability="dolibarr.other")
    doc["cases"][0]["expect"]["outputs"] = {"ghost": "1"}
    doc["cases"][2]["expect"] = {"status": "business_outcome", "outcome": "undeclared"}
    problems = suite_problems(Suite.model_validate(doc), cap)
    assert any("the suite is for" in p for p in problems)
    assert any("unknown output 'ghost'" in p for p in problems)
    assert any("not a declared outcome" in p for p in problems)


def _result(**over) -> RunResult:
    base = {"run_id": "r", "capability": "dolibarr.lookup_customer_outstanding", "version": "1.0.0", "lock_hash": None,
            "status": "success", "outcome": "success", "outputs": {"outstanding_amount": "75.37"}, "retry_safe": True}
    return RunResult.model_validate(base | over)


def test_compare_checks_status_outcome_outputs_recoveries_and_drift():
    case = Suite.model_validate(suite_doc()).cases[0]
    declared = ["outstanding_amount"]
    assert compare(case, _result(), {}, declared) == []
    assert compare(case, _result(outputs={"outstanding_amount": "1.00"}), {}, declared)
    assert compare(case, _result(outputs={}), {}, declared)
    assert compare(case, _result(status="business_outcome", outcome="customer_not_found", outputs={}), {}, declared)
    assert compare(case, _result(recoveries=[{"handler": "login", "state": "login", "step_id": None, "at_ms": 0}]),
                   {}, declared)
    drift = {"step_id": "s", "target": "t", "failed_strategies": [], "used_strategy": "css x"}
    assert compare(case, _result(drift=[drift]), {}, declared)
    allowed = case.model_copy(update={"expect": case.expect.model_copy(update={"allow_drift": True})})
    assert compare(allowed, _result(drift=[drift]), {}, declared) == []
    expects_login = case.model_copy(update={"expect": case.expect.model_copy(update={"recoveries": ["login"]})})
    assert compare(expects_login, _result(recoveries=[{"handler": "login", "state": "login", "step_id": None,
                                                       "at_ms": 0}]), {}, declared) == []


def test_ground_truth_beats_a_missing_literal():
    case = Suite.model_validate(suite_doc()).cases[1]
    assert compare(case, _result(outputs={"outstanding_amount": "1234.50"}), {"outstanding_amount": "1234.50"},
                   ["outstanding_amount"]) == []
    assert compare(case, _result(outputs={"outstanding_amount": "75.37"}), {"outstanding_amount": "1234.50"},
                   ["outstanding_amount"])


def test_extra_outputs_on_success_are_a_mismatch():
    case = Suite.model_validate(suite_doc()).cases[0]
    assert compare(case, _result(outputs={"outstanding_amount": "75.37", "extra": "1"}), {}, ["outstanding_amount"])
