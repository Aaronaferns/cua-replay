"""The resolver against the real Customer tab: the "Current outstanding bill" vs "... late" trap (spike S7), live."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "target" / "dolibarr"))

from dolirest import Rest, customer_by_code, outstanding_of  # noqa: E402

from livehelp import baseline_fixtures  # noqa: E402

from cua.artifact.names import names_match  # noqa: E402
from cua.artifact.schema import Fingerprint, Strategy, Target  # noqa: E402
from cua.config import settings  # noqa: E402
from cua.control.runtime import open_session  # noqa: E402
from cua.replay.resolver import ResolutionError, resolve  # noqa: E402
from cua.safety.policy import ActionRequest  # noqa: E402

pytestmark = [pytest.mark.live, pytest.mark.asyncio(loop_scope="module")]

LABEL = "Current outstanding bill"
TRAP_CUSTOMER = "CU1212-0005"


def _target(name: str, *, role: str = "generic") -> Target:
    strategy = Strategy(kind="role", role=role, name=name, why="test", matched_at_record="1 of 1")
    return Target(description=name, strategies=[strategy], fingerprint=Fingerprint(role=role, tag="div"))


@pytest.fixture(scope="module")
def baseline():
    return baseline_fixtures()


async def _customer_tab(session, code: str) -> None:
    admin = Rest.admin(settings())
    socid = customer_by_code(admin, code)["id"]
    url = f"{session.base_url}/comm/card.php?socid={socid}"
    request = ActionRequest(kind="navigate", actor="replay", page_url=session.base_url + "/", target_url=url,
                            effect="none", lease=session.lease())
    await session.surface.navigate(url, request)
    assert await session.surface.settle()


async def test_resolver_rejects_outstanding_late(baseline, tmp_path):
    """CU1212-0005 (demo data, untouched by the fixtures) owes both a current and a late amount, so its Customer tab
    carries "Current outstanding bill" and "Current outstanding bill late"."""
    async with open_session(run_id="live-resolver-late", evidence_root=tmp_path, actor="replay") as session:
        await _customer_tab(session, TRAP_CUSTOMER)
        surface = session.surface
        obs = await surface.observe(screenshot=False)
        # the trap is really on the page: a label that a substring match on "Current outstanding bill" would also hit
        assert f'"{LABEL} late"' in obs.snapshot

        exact = await resolve(surface, _target(LABEL), {})
        assert exact.index == 0 and exact.drift("s", "t") is None
        late = await resolve(surface, _target(f"{LABEL} late"), {})
        assert not await surface.same_element(exact.element, late.element)

        # a name that is only a part of the label never matches: no substring, no fuzzy fallback
        with pytest.raises(ResolutionError) as part:
            await resolve(surface, _target("outstanding bill"), {})
        assert part.value.code == "TARGET_NOT_FOUND"

        # the exact target carries the figure REST calls outstanding, never the late one
        admin = Rest.admin(settings())
        truth = str(outstanding_of(admin, customer_by_code(admin, TRAP_CUSTOMER)["id"]))
        text = await surface.read_text(exact.element)
        late_text = await surface.read_text(late.element)
        assert truth in text and truth not in late_text, (text, late_text)


SHARED_HELP = (
    "For searching inside text fields, you can use the characters ^ or $ to make a 'start or end with' search or use "
    "the ! to make a 'does not contain' test. You can use the | between two strings instead of a space for a 'OR' "
    "condition instead of 'AND'. For numeric values, you can use the operator <, >, <=, >= or != before the value, "
    "to filter using a mathematical comparison"
)


def _filter_target(*strategies: Strategy) -> Target:
    return Target(description="the Customer Code filter", strategies=list(strategies),
                  fingerprint=Fingerprint(role="textbox", tag="input", input_type="text",
                                          column_header="Customer Code"))


def _strategy(**kw) -> Strategy:
    return Strategy(why="test", matched_at_record="1 of 1", **kw)


async def test_fingerprint_blocks_misbound_fallback_live(baseline, tmp_path):
    """Every filter input of Dolibarr's customer list has the same long accessible name, so a name-based fallback
    cannot tell them apart. A fallback that finds exactly one filter, but the wrong column's, must be rejected."""
    async with open_session(run_id="live-resolver-filters", evidence_root=tmp_path, actor="replay") as session:
        url = f"{session.base_url}/societe/list.php?type=c"
        request = ActionRequest(kind="navigate", actor="replay", page_url=session.base_url + "/", target_url=url,
                                effect="none", lease=session.lease())
        await session.surface.navigate(url, request)
        assert await session.surface.settle()
        surface = session.surface
        gone = _strategy(kind="role", role="textbox", name="Customer Code filter that was renamed")
        shared = _strategy(kind="role", role="textbox", name=SHARED_HELP)
        by_code = _strategy(kind="css", css="input[name='search_customer_code']")
        by_name = _strategy(kind="css", css="input[name='search_nom']")

        # the shared name matches every filter input: never "the first of many"
        with pytest.raises(ResolutionError) as many:
            await resolve(surface, _filter_target(gone, shared), {})
        assert many.value.code == "TARGET_AMBIGUOUS" and "matches]" in many.value.detail

        # exactly one hit, but in the Third-party name column: the fingerprint rejects it
        with pytest.raises(ResolutionError) as wrong:
            await resolve(surface, _filter_target(gone, by_name), {})
        assert wrong.value.code == "TARGET_FINGERPRINT_MISMATCH"
        assert "column_header" in wrong.value.detail

        # the right fallback is accepted, and its use is reported as drift
        ok = await resolve(surface, _filter_target(gone, by_code), {})
        assert ok.index == 1 and ok.drift("s01", "code_filter") is not None
