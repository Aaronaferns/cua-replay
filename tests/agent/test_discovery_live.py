"""A scripted (non-model) discovery run on the live target: loop, recorder, locators and the compiler end to end.

This is the free rehearsal for the real model run: if the recorded trace of these moves does not compile, a model run
would not either."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "target" / "dolibarr"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from livetarget import reset_to_baseline  # noqa: E402
from scripts import GOAL, INPUT_SPECS, OUTPUT_SPECS, lookup_script  # noqa: E402

from cua.agent.discovery import discover  # noqa: E402
from cua.agent.scripted_adapter import ScriptedAdapter  # noqa: E402
from cua.control.runtime import open_session  # noqa: E402

pytestmark = [pytest.mark.live, pytest.mark.asyncio(loop_scope="module")]


@pytest.fixture(scope="module")
def baseline():
    return reset_to_baseline()


async def test_scripted_discovery_records_and_compiles(baseline, tmp_path):
    code = baseline.customer("alpha").code
    profile = os.environ.get("CUA_TEST_PROFILE", "apps/dolibarr/profile.json")
    async with open_session(run_id="scripted-alpha", evidence_root=tmp_path, actor="agent", profile=profile) as session:
        adapter = ScriptedAdapter(lookup_script(session.base_url))
        result = await discover(session, adapter, capability="lookup_customer_outstanding", goal=GOAL,
                                input_values={"customer_code": code}, input_specs=INPUT_SPECS,
                                output_specs=OUTPUT_SPECS, discovered_by="tester")
    assert result.loop.outcome == "done", result.loop.stop_reason
    assert "199.75" in result.loop.outputs["outstanding_amount"]
    if "customer_tab" not in session.profile.states:
        # the Customer tab (/comm/card.php, where the amount is) is a screen of the artifact owner's profile
        assert all("unknown in the app profile" in p for p in result.compile_problems)
        pytest.skip("the profile has no customer_tab state yet")
    assert result.compiled, result.compile_problems

    run = result.run_dir
    trace_text = (run / "trace.json").read_text(encoding="utf-8")
    assert code not in trace_text  # the persisted trace never holds the raw input
    assert code not in (run / "events.jsonl").read_text(encoding="utf-8")
    assert code not in (run / "capability.draft.json").read_text(encoding="utf-8")
    draft = json.loads((run / "capability.draft.json").read_text(encoding="utf-8"))
    assert [s["do"]["kind"] for s in draft["steps"]][-1] == "extract"
    assert any(s["do"]["kind"] == "assert" for s in draft["steps"])  # the identity check came from a sighting
    events = [json.loads(line) for line in (run / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(e["kind"] == "action" and e["rationale"] for e in events)
    assert list((run / "screenshots").glob("*.png"))
