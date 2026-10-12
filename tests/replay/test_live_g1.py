"""Gate G1, live: the compiled artifact replays a customer the model never saw, equals REST, and makes no model call.

The discovery run looked up alpha. This replays beta from the committed capability, in a fresh process, and checks
that no model SDK was ever imported (a model call is impossible without it).
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from livehelp import baseline_fixtures

from cua.config import REPO_ROOT

pytestmark = pytest.mark.live

SCRIPT = """
import json, sys
from cua.cli import main
rc = main(['replay', 'dolibarr.lookup_customer_outstanding', '--json', '--allow-draft', '--run-id', {run_id!r},
           '--input', 'customer_code={code}'])
print('RC=' + str(rc))
print('LOADED=' + json.dumps(sorted(m for m in sys.modules
      if m.split('.')[0] in ('anthropic', 'openai') or m.startswith(('cua.agent', 'cua.faults', 'cua.compiler')))))
"""


@pytest.fixture(scope="module")
def baseline():
    return baseline_fixtures()


def replay_in_fresh_process(code: str, run_id: str) -> tuple[dict, list[str]]:
    proc = subprocess.run([sys.executable, "-c", SCRIPT.format(run_id=run_id, code=code)], capture_output=True,
                          text=True, encoding="utf-8", cwd=REPO_ROOT, timeout=300)
    out = proc.stdout
    assert "RC=0" in out, out[-1500:] + proc.stderr[-1500:]
    return json.loads(out[: out.index("RC=")]), json.loads(out.split("LOADED=")[1].strip())


def test_compiled_artifact_replays_the_other_customer_with_no_model_call(baseline):
    from dolirest import Rest, customer_by_code, outstanding_of

    from cua.config import settings

    beta = baseline.customer("beta")
    admin = Rest.admin(settings())
    truth = str(outstanding_of(admin, customer_by_code(admin, beta.code)["id"]))

    result, loaded = replay_in_fresh_process(beta.code, "g1-beta-subprocess")

    assert result["status"] == "success", result.get("failure")
    assert result["outputs"]["outstanding_amount"] == truth == "355.20"
    assert result["drift"] == [] and result["recoveries"] == []
    # the CLI registers the `discover` command (a lazy shell), but no model SDK, loop, adapter, compiler or fault code
    assert "anthropic" not in loaded
    assert set(loaded) <= {"cua.agent", "cua.agent.commands"}, loaded
