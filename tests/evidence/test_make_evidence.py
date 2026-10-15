"""`cua make-evidence` offline: fakes for the target, the session and the replay; real suite, profile and writer."""

from __future__ import annotations

import argparse
import json
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from cua.artifact.lock import load_profile
from cua.config import REPO_ROOT, Settings
from cua.evidence.commands import (
    EvidenceCommandError,
    Hooks,
    build_faults,
    cmd_make_evidence,
    find_faults_file,
    load_faults,
    run_make_evidence,
)
from cua.evidence.writer import RunBundleWriter
from cua.replay.result import RunResult
from cua.safety.policy import load_policy

FIXTURES = REPO_ROOT / "tests" / "replay" / "fixtures"
AMOUNTS = {"CU2601-00901": "199.75", "CU2601-00902": "42.00"}


class FakeContext:
    def __init__(self):
        self.routes: list = []
        self.unrouted = 0

    async def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    async def unroute(self, pattern, handler):
        self.unrouted += 1


class FakeWorld:
    """The target, the session and the replay, with call logs."""

    def __init__(self, tmp_path: Path, *, wrong_amount: bool = False, explode_on: str | None = None):
        self.tmp_path = tmp_path
        self.wrong_amount = wrong_amount
        self.explode_on = explode_on
        self.resets = 0
        self.context = FakeContext()
        self.sessions: list[str] = []
        self.logins: list[bool] = []
        self.replay_kw: list[dict] = []
        self.profile = load_profile(REPO_ROOT / "apps" / "dolibarr" / "profile.json")
        self.policy = load_policy("dolibarr-sandbox", variables={"DOLIBARR_PORT": "8087"})

    def hooks(self) -> Hooks:
        def rest_view(case):
            oracles = {}
            for truth in case.expect.ground_truth:
                oracles[truth.output] = AMOUNTS.get(case.inputs[truth.input])
            return {"state_sha256": "abc123", "oracles": oracles}

        @asynccontextmanager
        async def open_session(*, run_id, profile, actor, evidence_root, login=True):
            self.sessions.append(run_id)
            self.logins.append(login)
            writer = RunBundleWriter(run_id, root=evidence_root)
            session = SimpleNamespace(
                surface=SimpleNamespace(page=SimpleNamespace(context=self.context)),
                guard=None, lease=lambda: None, evidence=writer, policy=self.policy, profile=self.profile,
                settings=Settings(cua_noperm_password="noperm-pass"), base_url="http://localhost:8087",
            )
            try:
                yield session
            finally:
                writer.close()

        async def replay(capability, profile, inputs, **kw):
            code = inputs["customer_code"]
            self.replay_kw.append(kw)
            if self.explode_on == code:
                raise RuntimeError(f"boom {code}")
            amount = AMOUNTS.get(code)
            if amount is None:
                return RunResult(run_id=kw["run_id"], capability=capability.id, version=capability.version,
                                 lock_hash=None, status="business_outcome", outcome="customer_not_found",
                                 retry_safe=True)
            if self.wrong_amount:
                amount = "0.01"
            return RunResult(run_id=kw["run_id"], capability=capability.id, version=capability.version,
                             lock_hash=None, status="success", outcome="success",
                             outputs={"outstanding_amount": amount}, retry_safe=True)

        def reset():
            self.resets += 1

        return Hooks(reset=reset, rest_view=rest_view, open_session=open_session, replay=replay)


def args_for(tmp_path: Path, **over) -> argparse.Namespace:
    values = {
        "capability": "dolibarr.lookup_customer_outstanding", "version": None, "cases": None,
        "out": str(tmp_path / "evidence"), "runs_root": str(tmp_path / "runs"), "capabilities_dir": str(FIXTURES),
        "handoff": False,
    }
    values.update(over)
    return argparse.Namespace(**values)


async def test_every_case_gets_a_bundle_with_ground_truth(tmp_path):
    world = FakeWorld(tmp_path)
    outcomes, code = await run_make_evidence(args_for(tmp_path), world.hooks())
    assert code == 0, [(o.case_id, o.mismatches, o.error) for o in outcomes]
    assert [o.case_id for o in outcomes] == ["alpha", "beta", "unknown_customer"]
    assert world.resets == 3  # one reset per case

    for case_id in ("alpha", "beta", "unknown_customer"):
        bundle = tmp_path / "evidence" / f"replay_{case_id}"
        truth = json.loads((bundle / "ground_truth.json").read_text(encoding="utf-8"))
        assert truth["case"] == case_id and truth["rest_state_unchanged"] is True
        assert truth["mismatches"] == []
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["status"] == "closed"
        assert {"ground_truth.json", "result.json"} <= {f["path"] for f in manifest["files"]}
    alpha = json.loads((tmp_path / "evidence/replay_alpha/ground_truth.json").read_text(encoding="utf-8"))
    assert alpha["before"]["oracles"] == {"outstanding_amount": "199.75"}
    assert alpha["actual"]["outputs"] == {"outstanding_amount": "199.75"}
    assert len(list((tmp_path / "runs").iterdir())) == 3  # the originals stay in runs/


async def test_a_run_that_disagrees_with_rest_is_reported_and_exits_1(tmp_path):
    outcomes, code = await run_make_evidence(args_for(tmp_path, cases="alpha"), FakeWorld(tmp_path, wrong_amount=True).hooks())
    assert code == 1
    assert any("outstanding_amount" in m for m in outcomes[0].mismatches)
    assert (tmp_path / "evidence/replay_alpha/ground_truth.json").exists()  # the evidence is still written


async def test_cases_filter_and_unknown_case(tmp_path):
    world = FakeWorld(tmp_path)
    outcomes, _ = await run_make_evidence(args_for(tmp_path, cases="beta"), world.hooks())
    assert [o.case_id for o in outcomes] == ["beta"] and world.resets == 1
    with pytest.raises(EvidenceCommandError, match="no such case"):
        await run_make_evidence(args_for(tmp_path, cases="nope"), world.hooks())


async def test_one_broken_case_does_not_stop_the_others_and_leaks_no_value(tmp_path):
    world = FakeWorld(tmp_path, explode_on="CU2601-00901")
    outcomes, code = await run_make_evidence(args_for(tmp_path), world.hooks())
    assert code == 1
    assert outcomes[0].error == "RuntimeError" and "CU2601" not in outcomes[0].error
    assert [o.ok for o in outcomes] == [False, True, True]


async def test_rerunning_replaces_the_bundle_folder(tmp_path):
    world = FakeWorld(tmp_path)
    await run_make_evidence(args_for(tmp_path, cases="alpha"), world.hooks())
    stale = tmp_path / "evidence/replay_alpha/stale.txt"
    stale.write_text("old", encoding="utf-8")
    await run_make_evidence(args_for(tmp_path, cases="alpha"), world.hooks())
    assert not stale.exists()


# ------------------------------------------------------------------------------------------------- faults sidecar


def folder_with_faults(tmp_path: Path, faults: dict | str) -> Path:
    root = tmp_path / "caps"
    shutil.copytree(FIXTURES / "dolibarr", root / "dolibarr")
    text = faults if isinstance(faults, str) else json.dumps(faults)
    (root / "dolibarr" / "lookup_customer_outstanding@1.0.0.faults.json").write_text(text, encoding="utf-8")
    return root


async def test_declared_faults_are_installed_around_the_replay_and_removed(tmp_path):
    root = folder_with_faults(tmp_path, {
        "schema": "cua.faults/v1", "capability": "dolibarr.lookup_customer_outstanding",
        "cases": {"alpha": [{"kind": "slow:250", "match": "/societe/**"},
                            {"kind": "interstitial", "match": "/comm/card.php", "times": 1}]},
    })
    world = FakeWorld(tmp_path)
    outcomes, code = await run_make_evidence(args_for(tmp_path, capabilities_dir=str(root)), world.hooks())
    assert code == 0, [(o.case_id, o.mismatches, o.error) for o in outcomes]
    assert len(world.context.routes) == 2  # alpha only
    assert world.context.unrouted == 2
    truth = json.loads((tmp_path / "evidence/replay_alpha/ground_truth.json").read_text(encoding="utf-8"))
    assert [f["kind"] for f in truth["faults"]] == ["slow:250", "interstitial"]
    beta = json.loads((tmp_path / "evidence/replay_beta/ground_truth.json").read_text(encoding="utf-8"))
    assert beta["faults"] == []


async def test_suite_case_faults_run_as_and_session_expiry_are_honoured(tmp_path, monkeypatch):
    root = tmp_path / "caps"
    shutil.copytree(FIXTURES / "dolibarr", root / "dolibarr")
    suite_path = root / "dolibarr" / "lookup_customer_outstanding.tests.json"
    doc = json.loads(suite_path.read_text(encoding="utf-8"))
    beta = next(c for c in doc["cases"] if c["id"] == "beta")
    beta.update(faults=[{"kind": "slow:250", "match": "/societe/**"}], run_as="noperm",
                expire_session_before="s02_open_card")
    suite_path.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr("cua.replay.commands._noperm_login", lambda: "noperm")
    expired = []
    monkeypatch.setattr("cua.evidence.commands._expire_session", lambda: expired.append(1))
    world = FakeWorld(tmp_path)
    outcomes, _ = await run_make_evidence(args_for(tmp_path, capabilities_dir=str(root), cases="beta"), world.hooks())
    assert outcomes[0].error is None, outcomes[0].error
    assert world.logins == [False] and len(world.context.routes) == 1
    truth = json.loads((tmp_path / "evidence/replay_beta/ground_truth.json").read_text(encoding="utf-8"))
    assert [f["kind"] for f in truth["faults"]] == ["slow:250"]
    before_step = world.replay_kw[0]["before_step"]
    for sid in ("s01_open_list", "s02_open_card", "s02_open_card"):
        await before_step(SimpleNamespace(id=sid))
    assert expired == [1]  # once, at the named step only


def test_faults_file_lookup_prefers_the_versioned_name(tmp_path):
    (tmp_path / "x.faults.json").write_text("{}", encoding="utf-8")
    assert find_faults_file("x", "1.0.0", tmp_path) == tmp_path / "x.faults.json"
    (tmp_path / "x@1.0.0.faults.json").write_text("{}", encoding="utf-8")
    assert find_faults_file("x", "1.0.0", tmp_path) == tmp_path / "x@1.0.0.faults.json"
    assert find_faults_file("y", "1.0.0", tmp_path) is None


def test_faults_file_validation(tmp_path):
    path = tmp_path / "f.faults.json"
    good = {"schema": "cua.faults/v1", "capability": "a.b", "cases": {"c1": []}}
    path.write_text(json.dumps(good), encoding="utf-8")
    assert load_faults(path, "a.b", {"c1"}) == {"c1": []}
    assert load_faults(None, "a.b", {"c1"}) == {}
    with pytest.raises(EvidenceCommandError, match="not 'x.y'"):
        load_faults(path, "x.y", {"c1"})
    with pytest.raises(EvidenceCommandError, match="does not have: c1"):
        load_faults(path, "a.b", {"other"})
    path.write_text(json.dumps({**good, "extra": 1}), encoding="utf-8")
    with pytest.raises(EvidenceCommandError, match="not a valid faults file"):
        load_faults(path, "a.b", {"c1"})


def test_build_faults_accepts_plan_notation_and_rejects_bad_specs():
    slow, dialog = build_faults([{"kind": "slow:250", "match": "/x/**"}, {"kind": "unknown_dialog", "match": "/y"}])
    assert slow.kind == "slow" and slow.ms == 250 and dialog.kind == "unknown_dialog"
    with pytest.raises(EvidenceCommandError, match="invalid fault"):
        build_faults([{"kind": "meteor", "match": "/x"}])
    with pytest.raises(EvidenceCommandError, match="string 'kind'"):
        build_faults([{"match": "/x"}])


async def test_a_bad_sidecar_stops_before_the_target_is_touched(tmp_path):
    root = folder_with_faults(tmp_path, "not json")
    world = FakeWorld(tmp_path)
    with pytest.raises(EvidenceCommandError):
        await run_make_evidence(args_for(tmp_path, capabilities_dir=str(root)), world.hooks())
    assert world.resets == 0 and world.sessions == []


def test_cli_registration_and_usage_error(tmp_path, capsys):
    from cua.cli import build_parser

    parsed = build_parser().parse_args(["make-evidence", "dolibarr.nothing", "--cases", "a,b", "--out", "x/"])
    assert parsed.func is cmd_make_evidence and parsed.cases == "a,b"
    parsed.capabilities_dir = str(FIXTURES)
    assert cmd_make_evidence(parsed) == 2
    assert "error:" in capsys.readouterr().err


def test_invoice_payments_view_counts_rest_payments():
    from cua.evidence.commands import _invoice_payments, _target_tools

    _target_tools()

    class Rest:
        def call(self, method, path):
            if path == "/invoices/ref/FA-1":
                return 200, {"id": "7"}
            assert path == "/invoices/7/payments"
            return 200, [{"amount": "10.5"}, {"amount": "3"}]

    assert _invoice_payments(Rest(), "FA-1") == {"invoice_id": "7", "count": 2, "amounts": ["10.5", "3"]}

    class NoPay(Rest):
        def call(self, method, path):
            return (200, {"id": "7"}) if "ref" in path else (404, {})

    assert _invoice_payments(NoPay(), "FA-1")["count"] == 0
    (fault,) = build_faults([{"kind": "lost_response_after_commit", "match": "/compta/paiement.php", "times": 1}])
    assert fault.kind == "lost_response_after_commit" and fault.status == 504


async def test_handoff_takes_exactly_one_case(tmp_path):
    with pytest.raises(EvidenceCommandError, match="exactly one case"):
        await run_make_evidence(args_for(tmp_path, handoff=True), hooks=Hooks(None, None, None, None))
