"""Replay on the real Dolibarr target: a hand-authored TEST-FIXTURE capability (tests/replay/fixtures, not
`capabilities/`) runs through the real surface, guard and session, and each amount equals the REST ground truth.

The REST helpers are test oracles only; replay itself never imports them.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "target" / "dolibarr"))

from dolirest import Rest, customer_by_code, outstanding_of  # noqa: E402
from livetarget import reset_to_baseline  # noqa: E402

from cua.artifact.lock import load_capability  # noqa: E402
from cua.config import REPO_ROOT, settings  # noqa: E402
from cua.control.runtime import open_session  # noqa: E402
from cua.replay.interpreter import Credentials, replay  # noqa: E402

pytestmark = pytest.mark.live
async_test = pytest.mark.asyncio(loop_scope="module")

FIXTURES = Path(__file__).parent / "fixtures"
CAPABILITY = FIXTURES / "dolibarr" / "lookup_customer_outstanding@1.0.0.json"


@pytest.fixture(scope="module")
def baseline():
    return reset_to_baseline()


def truth(code: str):
    admin = Rest.admin(settings())
    return outstanding_of(admin, customer_by_code(admin, code)["id"])


@pytest.mark.parametrize("key", ["alpha", "beta"])
@async_test
async def test_fixture_capability_replays_to_rest_ground_truth(baseline, key, tmp_path):
    customer = baseline.customer(key)
    capability = load_capability(CAPABILITY)
    async with open_session(run_id=f"live-replay-{key}", evidence_root=tmp_path, actor="replay") as session:
        cfg = session.settings
        creds = Credentials(cfg.cua_run_login, cfg.cua_run_password.get_secret_value())
        result = await replay(capability, session.profile, {"customer_code": customer.code},
                              surface=session.surface, guard=session.guard, lease=session.lease,
                              base_url=session.base_url, credentials=creds, evidence=session.evidence,
                              run_id=f"live-replay-{key}")
    assert result.status == "success", result.failure
    assert result.outputs["outstanding_amount"] == str(truth(customer.code))
    assert result.drift == [] and result.recoveries == [] and result.side_effects.commit_state == "none"


@async_test
async def test_unknown_customer_is_a_business_outcome_not_a_failure(baseline, tmp_path):
    capability = load_capability(CAPABILITY)
    async with open_session(run_id="live-replay-none", evidence_root=tmp_path) as session:
        result = await replay(capability, session.profile, {"customer_code": "CU0000-0000"},
                              surface=session.surface, guard=session.guard, lease=session.lease,
                              base_url=session.base_url, evidence=session.evidence, run_id="live-replay-none")
    assert result.status == "business_outcome" and result.outcome == "customer_not_found", result.failure


def test_cli_replay_loads_no_model_code(baseline, tmp_path):
    """The whole `cua replay` path in a fresh process: right amount, and no model SDK in sys.modules."""
    customer = baseline.customer("alpha")
    code = (
        "import json, sys\n"
        "from cua.cli import main\n"
        f"rc = main(['replay', 'dolibarr.lookup_customer_outstanding', '--json', '--allow-draft', '--run-id', 'live-replay-cli',"
        f" '--capabilities-dir', {str(FIXTURES)!r}, '--input', 'customer_code={customer.code}'])\n"
        "print('RC=' + str(rc))\n"
        "print('LOADED=' + json.dumps(sorted(m for m in sys.modules if m.split('.')[0] in "
        "('anthropic', 'openai', 'httpx', 'requests', 'cua.agent', 'cua.faults', 'cua.compiler'))))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8",
                          cwd=REPO_ROOT, timeout=240)
    out = proc.stdout
    assert "RC=0" in out, proc.stdout[-1500:] + proc.stderr[-1500:]
    result = json.loads(out[: out.index("RC=")])
    assert result["outputs"]["outstanding_amount"] == str(truth(customer.code))
    assert json.loads(out.split("LOADED=")[1].strip()) == []
