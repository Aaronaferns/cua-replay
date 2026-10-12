"""The navigation-fenced settle and `read_text` on a textbox / combobox (requests from the replay classifier)."""

from __future__ import annotations

import pytest
from test_web_surface import ref_of

from cua.artifact.schema import Strategy

pytestmark = pytest.mark.asyncio(loop_scope="module")

CUSTOMER_FILTER = "search_customer_code"


async def test_read_text_returns_the_current_value_of_textbox_and_combobox(open_page):
    surface, page = await open_page("/societe/list.php?type=c")
    await page.fill(f"input[name={CUSTOMER_FILTER}]", "CU1212-0005")
    await page.select_option("select[name=search_status]", "0")

    box = await surface.find(Strategy(kind="scoped", role="textbox", within_role="columnheader",
                                      within_name="Customer Code", why="t", matched_at_record="t"))
    assert len(box) == 1
    assert await surface.read_text(box[0]) == "CU1212-0005"

    combos = await surface.find(Strategy(kind="css", css="select[name=search_status]", why="t",
                                         matched_at_record="t"))
    assert len(combos) == 1
    assert await surface.read_text(combos[0]) == "Closed"


async def test_settle_waits_for_a_page_that_is_still_changing(open_page):
    surface, page = await open_page("/comm/card.php?socid=10")
    # the content keeps changing for ~700 ms, then stops
    await page.evaluate("""() => {
      let n = 0;
      const t = setInterval(() => {
        const p = document.createElement('p'); p.textContent = 'row ' + (++n);
        document.getElementById('id-right').appendChild(p);
        if (n === 7) { clearInterval(t); window.__done = true; }
      }, 100);
    }""")
    settled = await surface.settle()
    assert settled is True
    assert await page.evaluate("window.__done === true")


async def test_observe_settle_flag_returns_the_final_content(open_page):
    surface, page = await open_page("/comm/card.php?socid=10")
    await page.evaluate("""() => setTimeout(() => {
      const p = document.createElement('p'); p.textContent = 'late arrival';
      document.getElementById('id-right').appendChild(p);
    }, 250)""")
    obs = await surface.observe(screenshot=False, settle=True)
    assert "late arrival" in obs.snapshot
    assert ref_of(obs.snapshot, "cell", "Customer Code")


async def test_settle_gives_up_on_a_page_that_never_stops(open_page):
    surface, page = await open_page("/comm/card.php?socid=10")
    await page.evaluate("""() => { let n = 0; setInterval(() => {
      const p = document.createElement('p'); p.textContent = 'tick ' + (++n);
      document.getElementById('id-right').appendChild(p); }, 40); }""")
    assert await surface.settle(timeout_ms=800) is False
