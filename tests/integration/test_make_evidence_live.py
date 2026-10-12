"""`cua make-evidence` live on the Dolibarr target: the test-fixture capability under tests/replay/fixtures, bundles
written to a temp dir (never the repo's evidence/). Needs the target (skipped when it is not reachable)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from cua.config import REPO_ROOT
from cua.evidence.commands import run_make_evidence

pytestmark = pytest.mark.live

FIXTURES = REPO_ROOT / "tests" / "replay" / "fixtures"


async def test_make_evidence_regenerates_bundles_with_rest_ground_truth_and_declared_faults(tmp_path):
    caps = tmp_path / "caps"
    shutil.copytree(FIXTURES / "dolibarr", caps / "dolibarr")
    (caps / "dolibarr" / "lookup_customer_outstanding@1.0.0.faults.json").write_text(
        json.dumps({
            "schema": "cua.faults/v1",
            "capability": "dolibarr.lookup_customer_outstanding",
            "cases": {"alpha": [{"kind": "slow:100", "match": "/**"}]},
        }),
        encoding="utf-8",
    )
    import argparse

    args = argparse.Namespace(
        capability="dolibarr.lookup_customer_outstanding", version=None, cases="alpha,unknown_customer",
        out=str(tmp_path / "evidence"), runs_root=str(tmp_path / "runs"), capabilities_dir=str(caps),
    )
    outcomes, code = await run_make_evidence(args)
    assert code == 0, [(o.case_id, o.mismatches, o.error) for o in outcomes]

    alpha = tmp_path / "evidence" / "replay_alpha"
    truth = json.loads((alpha / "ground_truth.json").read_text(encoding="utf-8"))
    assert truth["actual"]["outputs"]["outstanding_amount"] == truth["after"]["oracles"]["outstanding_amount"]
    assert truth["rest_state_unchanged"] is True and truth["mismatches"] == []
    events = [json.loads(line) for line in (alpha / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    faults = [e for e in events if e["kind"] == "fault"]
    assert faults and faults[0]["detail"]["kind"] == "slow" and faults[0]["actor"] == "system"
    manifest = json.loads((alpha / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "closed"
    assert {"ground_truth.json", "result.json", "events.jsonl"} <= {f["path"] for f in manifest["files"]}

    missing = tmp_path / "evidence" / "replay_unknown_customer"
    assert json.loads((missing / "result.json").read_text(encoding="utf-8"))["outcome"] == "customer_not_found"
    assert not (REPO_ROOT / "evidence" / "replay_alpha").exists()
