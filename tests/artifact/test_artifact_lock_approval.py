"""Lock hash and approval sidecar: change control bound to what replay will actually do."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from sample_docs import capability, capability_dict, dolibarr_profile

from cua.artifact.approval import (
    Approval,
    SelfApprovalError,
    approval_state,
    approve,
    dumps_approval,
    load_approval,
    sidecar_path,
)
from cua.artifact.lock import canonical, content_hash, dumps, load_capability, load_profile, lock_hash
from cua.artifact.predicates import Text
from cua.artifact.schema import Capability, Meaning, Profile, State

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def test_lock_hash_is_over_capability_and_profile_only():
    cap, prof = capability(), dolibarr_profile()
    h = lock_hash(cap, prof)
    assert len(h) == 64
    assert h == lock_hash(cap, prof)
    import inspect

    assert list(inspect.signature(lock_hash).parameters) == ["capability", "profile"]


def test_lock_hash_crlf_stable(tmp_path: Path):
    cap, prof = capability(), dolibarr_profile()
    lf = tmp_path / "lf.json"
    crlf = tmp_path / "crlf.json"
    lf.write_bytes(dumps(cap).encode())
    crlf.write_bytes(dumps(cap).replace("\n", "\r\n").encode())
    compact = tmp_path / "compact.json"
    compact.write_text(json.dumps(json.loads(dumps(cap)), separators=(",", ":"), sort_keys=True))
    reordered = tmp_path / "reordered.json"
    reordered.write_text(json.dumps(json.loads(dumps(cap)), indent=4, sort_keys=True).replace("\n", "\r\n"))
    assert crlf.read_bytes() != lf.read_bytes()
    hashes = {lock_hash(load_capability(p), prof) for p in (lf, crlf, compact, reordered)}
    assert len(hashes) == 1 and hashes == {lock_hash(cap, prof)}
    # the profile file is subject to the same rule
    p_lf, p_crlf = tmp_path / "p_lf.json", tmp_path / "p_crlf.json"
    p_lf.write_bytes(dumps(prof).encode())
    p_crlf.write_bytes(dumps(prof).replace("\n", "\r\n").encode())
    assert lock_hash(cap, load_profile(p_lf)) == lock_hash(cap, load_profile(p_crlf))


def test_omitted_nulls_and_defaults_do_not_change_the_hash():
    doc = capability_dict()
    explicit = capability_dict()
    explicit["steps"][0]["verify"] = None
    explicit["steps"][0]["on"] = []
    explicit["contract"]["outcomes"] = {}
    prof = dolibarr_profile()
    assert lock_hash(Capability.model_validate(doc), prof) == lock_hash(Capability.model_validate(explicit), prof)


def test_hash_changes_with_any_behavior_change():
    prof = dolibarr_profile()
    base = lock_hash(capability(), prof)
    doc = capability_dict()
    doc["targets"]["outstanding"]["strategies"][0]["label"] = "Current outstanding bill late"
    assert lock_hash(Capability.model_validate(doc), prof) != base
    doc = capability_dict()
    doc["version"] = "1.0.1"
    assert lock_hash(Capability.model_validate(doc), prof) != base
    assert content_hash(capability()) != content_hash(prof)


def _edited_profile(prof: Profile) -> Profile:
    states = dict(prof.states)
    states["home"] = State(description="edited", signature=Text(equals="Anything"), origin="declared", edited=True)
    return prof.model_copy(update={"states": states})


def test_profile_edit_invalidates_approval():
    cap, prof = capability(), dolibarr_profile()
    appr = approve(cap, prof, approver="bob", at=NOW, validation_run_ids=["val-1"])
    assert approval_state(cap, prof, appr) == "approved"
    assert approval_state(cap, _edited_profile(prof), appr) == "stale"
    meaning = prof.model_copy(update={"default_meaning": {"login": Meaning(failure="OTHER_CODE")}})
    assert approval_state(cap, meaning, appr) == "stale"


def test_capability_edit_invalidates_approval():
    cap, prof = capability(), dolibarr_profile()
    appr = approve(cap, prof, approver="bob", at=NOW, validation_run_ids=["val-1"])
    doc = capability_dict()
    doc["steps"][3]["timeout_ms"] = 20_000
    assert approval_state(Capability.model_validate(doc), prof, appr) == "stale"
    assert approval_state(cap, prof, None) == "unapproved"


def test_self_approval_rejected():
    cap, prof = capability(), dolibarr_profile()
    assert cap.provenance.discovered_by == "alice"
    for who in ("alice", "  Alice ", "ALICE"):
        with pytest.raises(SelfApprovalError):
            approve(cap, prof, approver=who, at=NOW, validation_run_ids=["val-1"])
    # a sidecar written by hand with the discoverer as approver is not honored either
    forged = Approval(capability=cap.id, version=cap.version, lock_hash=lock_hash(cap, prof), approver="alice",
                      at=NOW, validation_run_ids=["v"])
    assert approval_state(cap, prof, forged) == "stale"


def test_approval_needs_validation_evidence_and_a_real_hash():
    cap, prof = capability(), dolibarr_profile()
    with pytest.raises(ValidationError):
        approve(cap, prof, approver="bob", at=NOW, validation_run_ids=[])
    with pytest.raises(ValidationError):
        Approval(capability=cap.id, version=cap.version, lock_hash="abc", approver="bob", at=NOW,
                 validation_run_ids=["v"])


def test_sidecar_is_separate_from_the_body_and_round_trips(tmp_path: Path):
    cap, prof = capability(), dolibarr_profile()
    appr = approve(cap, prof, approver="bob", at=NOW, validation_run_ids=["val-1", "val-2"])
    path = sidecar_path(tmp_path, cap)
    assert path == tmp_path / "dolibarr" / "lookup_customer_outstanding@1.0.0.approval.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(dumps_approval(appr).encode("utf-8"))
    assert b"\r" not in path.read_bytes() and path.read_bytes().endswith(b"}\n")
    assert load_approval(path) == appr
    assert load_approval(tmp_path / "missing.json") is None
    assert set(json.loads(path.read_text())) == {"capability", "version", "lock_hash", "approver", "at",
                                                 "validation_run_ids"}
    # approving a different version never counts for this one
    doc = capability_dict()
    doc["version"] = "1.1.0"
    assert approval_state(Capability.model_validate(doc), prof, appr) == "stale"


def test_canonical_is_key_order_independent():
    assert canonical({"b": 1, "a": [1, 2]}) == canonical({"a": [1, 2], "b": 1}) == b'{"a":[1,2],"b":1}'
