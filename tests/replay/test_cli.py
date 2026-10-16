"""`cua replay` and `cua compile` against a fake session (the real `open_session` is exercised by the live test)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fakes import BASE, FakeDolibarr, lease
from sample_docs import capability, capability_dict, dolibarr_profile
from trace_builder import build_trace

import cua.replay.commands as commands
from cua.artifact.lock import dumps
from cua.cli import main
from cua.config import Settings

CODE = "CU1212-0005"


@pytest.fixture
def caps(tmp_path, monkeypatch):
    monkeypatch.setattr(commands, "capabilities_dir", lambda: tmp_path / "capabilities")
    monkeypatch.setattr(commands, "approvals_dir", lambda: tmp_path / "approvals")
    monkeypatch.setattr(commands, "validation_dir", lambda: tmp_path / "validation")
    monkeypatch.setattr(commands, "evidence_dir", lambda: tmp_path)
    (tmp_path / "capabilities" / "dolibarr").mkdir(parents=True)
    return tmp_path / "capabilities" / "dolibarr"


@pytest.fixture
def sessions(monkeypatch):
    """Replace the session opener; records what the CLI asked for."""
    asked: list[dict] = []
    state = SimpleNamespace(fake=None)

    @asynccontextmanager
    async def fake_open_session(**kwargs):
        asked.append(kwargs)
        fake = state.fake or FakeDolibarr(commands_guard())
        yield SimpleNamespace(surface=fake, guard=fake.guard, lease=lease, base_url=BASE, evidence=None,
                              profile=dolibarr_profile(), settings=Settings())

    monkeypatch.setattr(commands, "_open_session", fake_open_session)
    return SimpleNamespace(asked=asked, state=state)


def commands_guard():
    from fakes import AllowGuard

    return AllowGuard()


def write(caps, name, cap=None):
    (caps / name).write_text(dumps(cap or capability()), encoding="utf-8", newline="\n")


def approved(cap=None, approver="bob"):
    """Write a valid approval sidecar for the capability under the (patched) approvals folder."""
    from datetime import UTC, datetime

    from cua.artifact.approval import approve, dumps_approval, sidecar_path

    cap = cap or capability()
    path = sidecar_path(commands.approvals_dir(), cap)
    path.parent.mkdir(parents=True, exist_ok=True)
    approval = approve(cap, dolibarr_profile(), approver=approver, at=datetime.now(UTC), validation_run_ids=["val-1"])
    path.write_bytes(dumps_approval(approval).encode("utf-8"))
    return path


def test_replay_success_prints_the_output_and_exits_zero(caps, sessions, capsys):
    write(caps, "lookup_customer_outstanding@1.0.0.json")
    approved()
    code = main(["replay", "dolibarr.lookup_customer_outstanding", "--input", f"customer_code={CODE}"])
    out = capsys.readouterr()
    assert code == 0
    assert "outstanding_amount = 75.37" in out.out and "success" in out.out
    assert sessions.asked[0]["actor"] == "replay" and sessions.asked[0]["headed"] is False
    assert sessions.asked[0]["profile"].replace("\\", "/").endswith("apps/dolibarr/profile.json")
    assert out.err == ""


def test_replay_json_prints_the_run_result(caps, sessions, capsys):
    write(caps, "lookup_customer_outstanding@1.0.0.json")
    assert main(["replay", "dolibarr.lookup_customer_outstanding", "--allow-draft", "--version", "1.0.0", "--json",
                 "--input", f"customer_code={CODE}"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "success" and result["outputs"] == {"outstanding_amount": "75.37"}


def test_replay_falls_back_to_the_draft_with_a_warning(caps, sessions, capsys):
    write(caps, "lookup_customer_outstanding@1.0.0.draft.json")
    assert main(["replay", "dolibarr.lookup_customer_outstanding", "--allow-draft", "--input", f"customer_code={CODE}"]) == 0
    assert "draft" in capsys.readouterr().err


def test_a_final_file_wins_over_its_draft_and_the_highest_version_wins(caps, sessions, capsys):
    write(caps, "lookup_customer_outstanding@1.0.0.json")
    write(caps, "lookup_customer_outstanding@1.0.0.draft.json")
    data = capability_dict() | {"version": "1.1.0"}
    from cua.artifact.schema import Capability

    write(caps, "lookup_customer_outstanding@1.1.0.draft.json", Capability.model_validate(data))
    path, draft = commands.find_capability_file("dolibarr.lookup_customer_outstanding", "1.0.0", caps.parent)
    assert path.name == "lookup_customer_outstanding@1.0.0.json" and not draft
    path, draft = commands.find_capability_file("dolibarr.lookup_customer_outstanding", None, caps.parent)
    assert path.name == "lookup_customer_outstanding@1.1.0.draft.json" and draft


def test_failure_exits_one_and_names_the_failure(caps, sessions, capsys):
    write(caps, "lookup_customer_outstanding@1.0.0.json")
    assert main(["replay", "dolibarr.lookup_customer_outstanding", "--allow-draft", "--input", "customer_code=not-a-code"]) == 1
    assert "INPUT_INVALID" in capsys.readouterr().out


def test_business_outcome_and_indeterminate_have_their_own_exit_codes():
    assert commands.EXIT_CODES == {"success": 0, "failure": 1, "business_outcome": 3, "indeterminate": 4}
    assert len(set(commands.EXIT_CODES.values()) | {commands.EXIT_USAGE}) == 5


def test_a_missing_capability_is_a_usage_error(caps, sessions, capsys):
    assert main(["replay", "dolibarr.nothing", "--input", "a=b"]) == 2
    assert "no capability file" in capsys.readouterr().err
    assert sessions.asked == []


def test_bad_arguments_are_usage_errors(caps, sessions, capsys):
    write(caps, "lookup_customer_outstanding@1.0.0.json")
    assert main(["replay", "lookup", "--input", "a=b"]) == 2
    assert main(["replay", "dolibarr.lookup_customer_outstanding", "--allow-draft", "--input", "novalue"]) == 2
    assert main(["replay", "dolibarr.lookup_customer_outstanding", "--allow-draft", "--input", "a=1", "--input", "a=2"]) == 2


def test_compile_writes_a_deterministic_draft(tmp_path, capsys):
    trace = tmp_path / "trace.json"
    trace.write_text(build_trace().model_dump_json(by_alias=True), encoding="utf-8")
    out1, out2 = tmp_path / "a" / "x.draft.json", tmp_path / "b.json"
    args = ["compile", str(trace), "--name", "dolibarr.lookup_customer_outstanding", "--out"]
    assert main([*args, str(out1)]) == 0 and main([*args, str(out2)]) == 0
    assert out1.read_bytes() == out2.read_bytes()
    assert json.loads(out1.read_text(encoding="utf-8"))["id"] == "dolibarr.lookup_customer_outstanding"
    assert b"\r" not in out1.read_bytes()


def test_compile_refuses_a_name_from_another_app(tmp_path, capsys):
    trace = tmp_path / "trace.json"
    trace.write_text(build_trace().model_dump_json(by_alias=True), encoding="utf-8")
    assert main(["compile", str(trace), "--name", "other.thing", "--out", str(tmp_path / "o.json")]) == 2
    assert not (tmp_path / "o.json").exists()


def test_a_failed_login_is_one_line_naming_the_port_not_a_traceback(capsys):
    from cua.surface.login import LoginFailed

    def boom(args):
        raise LoginFailed("the login form is still shown after submitting")

    assert commands._usage_errors(boom)(None) == commands.EXIT_USAGE
    err = capsys.readouterr().err
    assert err.startswith("error: login failed") and "DOLIBARR_PORT" in err and "Traceback" not in err
