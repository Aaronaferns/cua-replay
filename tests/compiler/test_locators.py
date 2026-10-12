"""Locator candidates are recorded on the engine replay uses, and only kept when they find exactly the element."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "surface"))
from fakesite import ORIGIN, FakeGuard, serve  # noqa: E402

from cua.artifact.schema import Fingerprint  # noqa: E402
from cua.compiler.locators import proposals, record_candidates, tokenize  # noqa: E402
from cua.compiler.recorder import Recorder  # noqa: E402
from cua.surface import snapshot as snap  # noqa: E402
from cua.surface.web import DOLIBARR_SCOPE, WebSurface  # noqa: E402

pytestmark = pytest.mark.asyncio(loop_scope="module")
HELP = "For searching inside text fields"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def browser():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        yield b
        await b.close()


@pytest_asyncio.fixture(loop_scope="module")
async def open_page(browser):
    contexts = []

    async def _open(path: str):
        ctx = await browser.new_context(viewport={"width": 1366, "height": 900}, device_scale_factor=1,
                                        service_workers="block", accept_downloads=False)
        contexts.append(ctx)
        await serve(ctx)
        page = await ctx.new_page()
        await page.goto(ORIGIN + path)
        return WebSurface(page, FakeGuard(allow=True), scope=DOLIBARR_SCOPE)

    yield _open
    for ctx in contexts:
        await ctx.close()


def recorder(surface, values=None):
    return Recorder(surface, run_id="r", app="dolibarr", goal="g", model="m", inputs={}, outputs={},
                    input_values=values or {}, entry_url=ORIGIN + "/index.php")


async def record_ref(surface, role, name_part=None, nth=0, values=None):
    obs = await surface.observe(screenshot=False)
    nodes = [n for n in snap.parse(obs.snapshot) if n.role == role and n.ref and (name_part is None or (n.name or "").startswith(name_part))]
    node = nodes[nth]
    element = await surface.element_for_ref(node.ref, obs.epoch)
    target = await recorder(surface, values).target(element, ref=node.ref, role=node.role, name=node.name, dialog=None)
    return element, target


async def test_candidate_same_element_as_ref(open_page):
    """Every kept candidate, searched afresh, finds exactly one element and it is the one the ref pointed at."""
    surface = await open_page("/societe/list.php?type=c")
    element, target = await record_ref(surface, "textbox", HELP, nth=3)  # the Customer Code filter
    assert target.candidates
    for cand in target.candidates:
        assert cand.match_count == 1 and cand.same_element
        found = await surface.find(cand.strategy)
        assert len(found) == 1 and await surface.same_element(found[0], element)
    kinds = [c.strategy.kind for c in target.candidates]
    assert "scoped" in kinds and "css" in kinds
    assert kinds == sorted(kinds, key=lambda k: {"role": 1, "same_row": 2, "table_cell": 3, "scoped": 4, "css": 6}[k])
    assert [c.rank for c in target.candidates] == sorted(c.rank for c in target.candidates)
    assert target.fingerprint.column_header == "Customer Code"


async def test_shared_name_filter_has_no_role_candidate(open_page):
    """The filter textboxes share one accessible name (6 matches), so a role + name candidate is not kept."""
    surface = await open_page("/societe/list.php?type=c")
    _, target = await record_ref(surface, "textbox", HELP, nth=3)
    assert "role" not in {c.strategy.kind for c in target.candidates}
    scoped = [c for c in target.candidates if c.strategy.kind == "scoped"]
    assert scoped and all(c.strategy.within_name == "Customer Code" for c in scoped)


async def test_unique_control_gets_a_role_candidate(open_page):
    surface = await open_page("/societe/list.php?type=c")
    element, target = await record_ref(surface, "link", "Northwind Test")
    assert target.candidates[0].rank == 1 and target.candidates[0].strategy.kind == "role"
    assert target.candidates[0].strategy.name == "Northwind Test"


async def test_value_cell_is_found_from_its_label_by_same_row(open_page):
    surface = await open_page("/comm/card.php?socid=10")
    obs = await surface.observe(screenshot=False)
    rec = recorder(surface, {"customer_code": "CU1212-0005"})
    wanted = rec.sighting_refs(obs)
    assert [t for t, _ in wanted] == ["customer_code"]
    sightings = await rec.sightings(obs, wanted)
    assert len(sightings) == 1
    kept = sightings[0].target.candidates
    same_row = [c for c in kept if c.strategy.kind == "same_row"]
    assert same_row and same_row[0].strategy.label == "Customer Code" and same_row[0].strategy.take == "value"
    # no candidate holds the raw value; the row key, if any, is the token
    assert "CU1212-0005" not in "".join(c.strategy.model_dump_json() for c in kept)


async def test_candidates_that_find_a_different_element_are_dropped(open_page):
    surface = await open_page("/societe/list.php?type=c")
    element, target = await record_ref(surface, "textbox", HELP, nth=3)
    # Propose a strategy that matches exactly one element, but another one: the second filter's column.
    fp = Fingerprint(role="textbox", tag="input", input_type="text", column_header="Alias Name")
    wrong = await record_candidates(surface, element, role="textbox", name=None, fingerprint=fp, inputs={})
    for cand in wrong:
        found = await surface.find(cand.strategy)
        assert await surface.same_element(found[0], element)  # whatever survived is really the element
    assert not any(c.strategy.kind == "scoped" and c.strategy.within_name == "Alias Name" for c in wrong)


def test_tokenize_replaces_values_with_tokens():
    assert tokenize("row CU1-23 ok", {"customer_code": "CU1-23"}) == "row {{customer_code}} ok"
    assert tokenize("nothing", {"customer_code": "CU1-23"}) == "nothing"


def test_proposals_are_ranked_and_value_free():
    fp = Fingerprint(role="link", tag="a", row_label="Customer Code", column_header="Third-party name")
    out = proposals(role="link", name="Acme", fingerprint=fp, inputs={"customer_code": "X"}, dialog=None,
                    css=["a.big"])
    ranks = [r for r, _ in out]
    assert ranks == sorted(ranks) and ranks[0] == 1 and ranks[-1] == 6
    assert any(s.kind == "table_cell" and s.row_key == "{{customer_code}}" for _, s in out)
    assert all("why" not in s.locator_strings() for _, s in out)
