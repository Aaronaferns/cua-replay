"""`cua validate` on the real target: reset per case, real replay, REST ground truth (fixtures path only)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import cua.replay.commands as commands
from cua.cli import main

pytestmark = pytest.mark.live

FIXTURES = Path(__file__).parent / "fixtures"


def test_validate_the_fixture_capability_live(monkeypatch, tmp_path):
    monkeypatch.setattr(commands, "capabilities_dir", lambda: FIXTURES)
    monkeypatch.setattr(commands, "validation_dir", lambda: tmp_path)
    code = main(["validate", "dolibarr.lookup_customer_outstanding", "--run-id", "val-live"])
    summary = json.loads((tmp_path / "val-live" / "summary.json").read_text(encoding="utf-8"))
    assert code == 0, summary["cases"]
    assert [(c["case_id"], c["status"], c["outcome"]) for c in summary["cases"]] == [
        ("alpha", "success", "success"), ("beta", "success", "success"),
        ("unknown_customer", "business_outcome", "customer_not_found")]
    assert summary["passed"] is True and len(summary["lock_hash"]) == 64
