"""Dolibarr's payment form (nameless amount box, header row of plain cells, invoice ref inside a link) and a commit
click whose navigation is slow."""

from __future__ import annotations

import asyncio

import pytest
from fakesite import ORIGIN, request

from cua.artifact.schema import Strategy
from cua.compiler.locators import record_candidates

pytestmark = pytest.mark.asyncio(loop_scope="module")

FORM = """<main id="id-right"><table><tr class="liste_titre"><td>Invoice</td><td>Amount</td><td>Payment amount</td></tr>
<tr><td><a href="/i/%(a)s"> %(a)s</a></td><td>10.00</td><td><input type="text" name="amount_458"></td></tr>
<tr><td><a href="/i/%(b)s"> %(b)s</a></td><td>20.00</td><td><input type="text" name="amount_459"></td></tr></table></main>"""


async def test_nameless_box_recorded_by_column_and_invoice_row(open_page):
    surface, page = await open_page("/")
    await page.set_content(FORM % {"a": "FA2610-0037", "b": "FA2610-0038"})
    el = (await surface.find(Strategy(kind="css", css="input[name=amount_458]", why="t", matched_at_record="t")))[0]
    fp = (await surface.fingerprint(el)).model_copy(update={"column_header": "Payment amount"})
    cands = await record_candidates(surface, el, role="textbox", name=None, fingerprint=fp,
                                    inputs={"invoice_ref": "FA2610-0037", "amount": "5.00"})
    keyed = [c.strategy for c in cands if c.strategy.kind == "table_cell" and c.strategy.row_key]
    assert [(s.column, s.row_key, s.role) for s in keyed] == [("Payment amount", "{{invoice_ref}}", "textbox")]

    await page.set_content(FORM % {"a": "FA2611-0001", "b": "FA2611-0002"})
    found = await surface.find(keyed[0].model_copy(update={"row_key": "FA2611-0002"}))
    assert len(found) == 1 and await found[0].locator.get_attribute("name") == "amount_459"


async def test_commit_click_survives_a_slow_navigation(context, open_page):
    async def slow(route):
        await asyncio.sleep(1.5)
        await route.fulfill(status=200, content_type="text/html", body="<title>Done</title>done")

    await context.route(f"{ORIGIN}/slow", slow)
    surface, page = await open_page("/")
    await page.set_content(f'<form action="{ORIGIN}/slow" method="post"><button>Validate</button></form>')
    surface._action_timeout = 300  # a click that waited on the navigation would time out
    button = (await surface.find(Strategy(kind="role", role="button", name="Validate", why="t", matched_at_record="t")))[0]
    decision = await surface.click(button, request("click", page.url, effect="commit"))
    assert decision.allowed
