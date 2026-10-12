"""Spike S5, live: how many template signatures (URL glob + first main-content heading) survive a different customer?

The template states are built from the committed discovery trace (alpha). The compiled capability is then replayed
for beta, every screen replay observed is kept, and each template is checked against beta's screen of the same state.
The target is at most one edit per capability. The measured number is asserted, so a drift in the template, the
profile or the application shows up here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from livehelp import baseline_fixtures

from cua.artifact.classify import classify
from cua.artifact.lock import load_capability, load_profile
from cua.artifact.propose import survival_edits
from cua.artifact.screen import view_from_outline
from cua.compiler.compile import template_signatures
from cua.compiler.trace import RecordedTrace
from cua.config import REPO_ROOT
from cua.control.runtime import open_session
from cua.replay.interpreter import Credentials, replay

pytestmark = pytest.mark.live
async_test = pytest.mark.asyncio(loop_scope="module")

TRACE = REPO_ROOT / "evidence" / "discovery_lookup_customer_outstanding" / "trace.json"
PROFILE = REPO_ROOT / "apps" / "dolibarr" / "profile.json"
CAPABILITY = REPO_ROOT / "capabilities" / "dolibarr" / "lookup_customer_outstanding@1.0.0.json"

MAX_EDITS = 1  # the S5 success criterion, per capability


class Recording:
    """Delegates to the real surface and keeps every observation (the replay run is otherwise untouched)."""

    def __init__(self, inner):
        self._inner = inner
        self.observations = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def observe(self, **kwargs):
        obs = await self._inner.observe(**kwargs)
        self.observations.append(obs)
        return obs


@pytest.fixture(scope="module")
def baseline():
    return baseline_fixtures()


@async_test
async def test_s5_template_signatures_survive_a_different_customer(baseline, tmp_path):
    profile = load_profile(PROFILE)
    trace = RecordedTrace.model_validate_json(TRACE.read_bytes())
    templates = template_signatures(trace, profile)
    assert sorted(templates) == ["customer_list", "customer_tab", "home"]

    beta = baseline.customer("beta")
    async with open_session(run_id="live-s5-beta", evidence_root=tmp_path, actor="replay") as session:
        surface = Recording(session.surface)
        cfg = session.settings
        creds = Credentials(cfg.cua_run_login, cfg.cua_run_password.get_secret_value())
        result = await replay(load_capability(CAPABILITY), session.profile, {"customer_code": beta.code},
                              surface=surface, guard=session.guard, lease=session.lease, base_url=session.base_url,
                              credentials=creds, evidence=session.evidence, run_id="live-s5-beta")
    assert result.status == "success", result.failure

    runs = []
    for obs in surface.observations:
        view = view_from_outline(obs.facts.url, obs.snapshot)
        got = classify(profile, view)  # the catalog's own answer names the state this screen is
        if got.kind == "state" and got.state in templates:
            runs.append((view, got.state))
    assert {state for _, state in runs} == set(templates)  # every template was exercised by beta's run

    edits = survival_edits(templates, runs)
    assert len(edits) <= MAX_EDITS, edits
    assert edits == []  # measured: all three template signatures survive unedited
