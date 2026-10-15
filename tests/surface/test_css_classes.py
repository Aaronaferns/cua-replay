"""A value shown only as the text of a multi-class element (Dolibarr's payment card banner ref) is recorded by a
class, never by the value, and found again on a page showing a different value."""

from __future__ import annotations

import pytest

from cua.artifact.schema import Strategy
from cua.compiler.locators import record_candidates

pytestmark = pytest.mark.asyncio(loop_scope="module")

CARD = ('<main id="id-right"><div class="arearef"><a href="#">Back</a><div class="inline-block floatleft refid">%s</div>'
        '<div class="inline-block floatleft">other</div></div></main>')


async def test_banner_ref_recorded_by_a_unique_class(open_page):
    surface, page = await open_page("/")
    await page.set_content(CARD % "PAY2610-0028")
    el = (await surface.find(Strategy(kind="css", css="div.refid", why="t", matched_at_record="t")))[0]
    fp = await surface.fingerprint(el)
    cands = await record_candidates(surface, el, role="generic", name="PAY2610-0028", fingerprint=fp, inputs={})
    css = [c.strategy.css for c in cands if c.strategy.kind == "css"]
    assert css == ["div.refid"]  # "inline-block"/"floatleft" match two elements and are dropped
    assert not any("PAY2610" in s for c in cands for s in c.strategy.locator_strings() if c.strategy.kind == "css")

    await page.set_content(CARD % "PAY2611-0031")
    found = await surface.find(cands[-1].strategy)
    assert len(found) == 1 and await surface.read_text(found[0]) == "PAY2611-0031"
