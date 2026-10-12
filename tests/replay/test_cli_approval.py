"""`cua approve`, `cua capabilities` and approval gating in `cua replay`, against a fake session."""

from __future__ import annotations

import json

import pytest
from sample_docs import capability, capability_dict, dolibarr_profile
from test_cli import CODE, approved, caps, sessions, write  # noqa: F401  (fixtures)

import cua.replay.commands as commands
from cua.artifact.approval import approval_state, load_approval, sidecar_path
from cua.artifact.lock import lock_hash
from cua.artifact.schema import Capability
from cua.cli import main

REPLAY = ["replay", "dolibarr.lookup_customer_outstanding", "--input", f"customer_code={CODE}"]
NAME = "lookup_customer_outstanding@1.0.0"


def write_summary(run_id="val-1", *, passed=True, lock=None, version="1.0.0"):
    out = commands.validation_dir() / run_id
    out.mkdir(parents=True, exist_ok=True)
    doc = {
        "schema": "cua.validation/v1", "run_id": run_id, "capability": "dolibarr.lookup_customer_outstanding",
        "version": version, "lock_hash": lock or lock_hash(capability(), dolibarr_profile()), "repeat": 1,
        "started_at": "2026-10-12T12:00:00Z", "passed": passed, "cases": [],
    }
    (out / "summary.json").write_text(json.dumps(doc), encoding="utf-8")


def approve_args(*runs, approver="bob"):
    return ["approve", "dolibarr.lookup_customer_outstanding", "--version", "1.0.0", "--approver", approver,
            *[a for r in runs for a in ("--run", r)]]


# --- gating ----------------------------------------------------------------------------------------------------


def test_replay_refuses_an_unapproved_capability(caps, sessions, capsys):
    write(caps, f"{NAME}.json")
    assert main(REPLAY) == 2
    err = capsys.readouterr().err
    assert "unapproved" in err and "--allow-draft" in err
    assert sessions.asked == []  # nothing was opened, nothing ran


def test_allow_draft_runs_it_with_a_clear_warning(caps, sessions, capsys):
    write(caps, f"{NAME}.json")
    assert main([*REPLAY, "--allow-draft"]) == 0
    assert "warning" in capsys.readouterr().err


def test_replay_refuses_a_stale_approval(caps, sessions, capsys):
    write(caps, f"{NAME}.json")
    approved()
    edited = capability_dict()
    edited["steps"][3]["timeout_ms"] = 20_000
    write(caps, f"{NAME}.json", Capability.model_validate(edited))  # edited after approval
    assert main(REPLAY) == 2
    assert "stale" in capsys.readouterr().err and sessions.asked == []
    assert main([*REPLAY, "--allow-draft"]) == 0


def test_a_draft_file_is_never_approved_even_with_a_matching_sidecar(caps, sessions, capsys):
    write(caps, f"{NAME}.draft.json")
    approved()
    assert main(REPLAY) == 2
    assert "draft" in capsys.readouterr().err and sessions.asked == []
    assert main([*REPLAY, "--allow-draft"]) == 0


def test_a_profile_edit_makes_the_approval_stale(caps, sessions, tmp_path):
    cap = capability()
    path = approved(cap)
    appr = load_approval(path)
    edited = dolibarr_profile().model_copy(update={"default_meaning": {}})
    assert approval_state(cap, dolibarr_profile(), appr) == "approved"
    assert approval_state(cap, edited, appr) == "stale"


# --- approve ---------------------------------------------------------------------------------------------------


def test_approve_writes_the_sidecar_and_replay_then_runs_without_a_flag(caps, sessions, capsys):
    write(caps, f"{NAME}.json")
    write_summary("val-1")
    assert main(approve_args("val-1")) == 0
    path = sidecar_path(commands.approvals_dir(), capability())
    assert path.name == f"{NAME}.approval.json" and path.parent.name == "dolibarr"
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["approver"] == "bob" and doc["validation_run_ids"] == ["val-1"]
    assert doc["lock_hash"] == lock_hash(capability(), dolibarr_profile())
    capsys.readouterr()
    assert main(REPLAY) == 0 and capsys.readouterr().err == ""


def test_self_approval_is_refused_and_writes_nothing(caps, sessions, capsys):
    write(caps, f"{NAME}.json")
    write_summary("val-1")
    assert capability().provenance.discovered_by == "alice"
    assert main(approve_args("val-1", approver="Alice")) == 1
    assert "ran the discovery" in capsys.readouterr().err
    assert not sidecar_path(commands.approvals_dir(), capability()).exists()


@pytest.mark.parametrize("summary", [
    {"passed": False},
    {"lock": "0" * 64},  # validated against another lock hash: edited since
    {"version": "1.1.0"},
])
def test_approval_needs_a_passing_validation_of_this_lock_hash(caps, sessions, capsys, summary):
    write(caps, f"{NAME}.json")
    write_summary("val-1", **summary)
    assert main(approve_args("val-1")) == 1
    assert "validation run" in capsys.readouterr().err
    assert not sidecar_path(commands.approvals_dir(), capability()).exists()


def test_approval_cites_runs_that_exist(caps, sessions, capsys):
    write(caps, f"{NAME}.json")
    assert main(approve_args("val-missing")) == 1
    assert "no summary" in capsys.readouterr().err


def test_a_draft_cannot_be_approved(caps, sessions, capsys):
    write(caps, f"{NAME}.draft.json")
    write_summary("val-1")
    assert main(approve_args("val-1")) == 2
    assert "draft" in capsys.readouterr().err


def test_approve_needs_a_run_and_a_version(caps, sessions):
    write(caps, f"{NAME}.json")
    with pytest.raises(SystemExit):
        main(["approve", "dolibarr.lookup_customer_outstanding", "--version", "1.0.0", "--approver", "bob"])


# --- capabilities ----------------------------------------------------------------------------------------------


def test_capabilities_json_lists_version_effect_state_and_lock_hash(caps, capsys):
    write(caps, f"{NAME}.json")
    assert main(["capabilities", "--json"]) == 0
    (row,) = json.loads(capsys.readouterr().out)
    assert row["id"] == "dolibarr.lookup_customer_outstanding" and row["version"] == "1.0.0"
    assert row["effect"] == "read" and row["approval"] == "unapproved" and row["draft"] is False
    assert row["lock_hash"] == lock_hash(capability(), dolibarr_profile())

    approved()
    main(["capabilities", "--json"])
    assert json.loads(capsys.readouterr().out)[0]["approval"] == "approved"

    edited = capability_dict()
    edited["steps"][3]["timeout_ms"] = 20_000
    write(caps, f"{NAME}.json", Capability.model_validate(edited))
    main(["capabilities", "--json"])
    assert json.loads(capsys.readouterr().out)[0]["approval"] == "stale"


def test_capabilities_lists_drafts_and_survives_a_broken_file(caps, capsys):
    write(caps, "lookup_customer_outstanding@1.0.0.draft.json")
    (caps / "broken@1.0.0.json").write_text("{not json", encoding="utf-8")
    assert main(["capabilities", "--json"]) == 0
    rows = {r["id"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["dolibarr.lookup_customer_outstanding"]["draft"] is True
    assert rows["dolibarr.lookup_customer_outstanding"]["approval"] == "unapproved"
    assert rows["dolibarr.broken"]["error"] and rows["dolibarr.broken"]["approval"] == "unapproved"


def test_capabilities_text_output(caps, capsys):
    write(caps, f"{NAME}.json")
    assert main(["capabilities"]) == 0
    assert "dolibarr.lookup_customer_outstanding@1.0.0" in capsys.readouterr().out


def test_review_writes_a_deterministic_card_beside_the_capability(caps, capsys):
    write(caps, f"{NAME}.json")
    assert main(["review", "dolibarr.lookup_customer_outstanding"]) == 0
    card = caps / "lookup_customer_outstanding@1.0.0.review.md"
    first = card.read_bytes()
    assert lock_hash(capability(), dolibarr_profile()).encode() in first and b"\r" not in first
    assert main(["review", "dolibarr.lookup_customer_outstanding"]) == 0 and card.read_bytes() == first
    assert main(["review", "dolibarr.nothing"]) == 2
