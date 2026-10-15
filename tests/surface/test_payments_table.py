"""Dolibarr's invoice card after a payment: a payments table whose header row is plain cells. The new payment's link,
its ref cell and its amount are recorded by column + a row keyed by an input (never by the payment ref), and the
screen after the commit sights the amount in that row (the confirmation echo)."""

from __future__ import annotations

import pytest

from cua.artifact.schema import Strategy
from cua.compiler.locators import record_candidates
from cua.compiler.recorder import Recorder
from cua.surface.web import WebElement

pytestmark = pytest.mark.asyncio(loop_scope="module")

CARD = """<main id="id-right"><table><tr><td>Billed</td><td>120.40</td></tr></table>
<table class="paymenttable"><tr class="liste_titre"><td>Payments</td><td>Date</td><td>Type</td><td>Bank account</td>
<td></td><td>Amount</td></tr>
%(rows)s
<tr><td colspan="4">Already paid</td><td>%(paid)s</td><td></td></tr>
<tr><td colspan="4">Remaining unpaid</td><td>70.40</td><td></td></tr></table></main>"""
ROW = ('<tr><td><a href="/p/%(id)s">%(ref)s</a></td><td>10/15/2026</td><td>%(method)s</td>'
       '<td><a href="/b/9">CUASEED</a></td><td><a href="/del/%(id)s">x</a></td><td>%(amount)s</td></tr>')
VALUES = {"invoice_ref": "FA2610-0037", "amount": "50.00", "method": "Cash", "bank_account": "CUASEED"}


def _card(*rows: dict, paid: str) -> str:
    return CARD % {"rows": "".join(ROW % r for r in rows), "paid": paid}


def _css(css: str) -> Strategy:
    return Strategy(kind="css", css=css, why="t", matched_at_record="t")


async def test_new_payment_link_and_ref_are_recorded_structurally(open_page):
    surface, page = await open_page("/")
    await page.set_content(_card({"id": 89, "ref": "PAY2610-0028", "method": "Cash", "amount": "50.00"}, paid="50.00"))
    link = (await surface.find(_css("a[href='/p/89']")))[0]
    fp = await surface.fingerprint(link)
    assert fp.column_header == "Payments"
    cands = await record_candidates(surface, link, role="link", name="PAY2610-0028", fingerprint=fp, inputs=VALUES)
    keyed = [c.strategy for c in cands if c.strategy.kind == "table_cell" and c.strategy.row_key]
    assert ("Payments", "{{method}}", "link") in [(s.column, s.row_key, s.role) for s in keyed]

    cell = (await surface.find(_css("a[href='/p/89']")))[0].locator.locator("xpath=..")
    cell_fp = await surface.fingerprint(WebElement(cell, "t", "cell", None))
    assert cell_fp.column_header == "Payments"

    # Another payment, an earlier one of a different method on the same invoice: the keyed finder finds the new one.
    await page.set_content(_card({"id": 70, "ref": "PAY2609-0001", "method": "Cheque", "amount": "20.00"},
                                 {"id": 90, "ref": "PAY2611-0031", "method": "Cash", "amount": "50.00"}, paid="70.00"))
    s = next(s for s in keyed if s.row_key == "{{method}}").model_copy(update={"row_key": "Cash"})
    found = await surface.find(s)
    assert len(found) == 1 and await surface.read_text(found[0]) == "PAY2611-0031"


async def test_label_value_rows_keep_their_label_and_get_no_header(open_page):
    surface, page = await open_page("/")
    await page.set_content(_card({"id": 89, "ref": "PAY2610-0028", "method": "Cash", "amount": "50.00"}, paid="50.00"))
    paid = page.locator("xpath=//td[text()='Already paid']/following-sibling::td[1]")
    fp = await surface.fingerprint(WebElement(paid, "t", "cell", None))
    assert fp.row_label == "Already paid" and fp.column_header is None


async def test_the_screen_after_a_commit_sights_the_amount_in_the_new_payment_row(open_page):
    surface, page = await open_page("/")
    await page.set_content(_card({"id": 89, "ref": "PAY2610-0028", "method": "Cash", "amount": "50.00"}, paid="49.00"))
    rec = Recorder(surface, run_id="r", app="dolibarr", goal="g", model="m", inputs={}, outputs={},
                   input_values=VALUES, entry_url="http://x/")
    obs = await surface.observe(screenshot=False)
    assert await rec.sightings(obs, rec.sighting_refs(obs)) == []  # an ordinary screen: no label/value row shows it

    obs = await surface.observe(screenshot=False)
    found = await rec.sightings(obs, rec.sighting_refs(obs, after_commit=True), after_commit=True)
    by_token = {s.token: s.target for s in found}
    assert "amount" in by_token
    strategies = [c.strategy for c in by_token["amount"].candidates]
    assert all(s.row_key != "{{amount}}" for s in strategies)  # never keyed by the very value it echoes
    assert ("Amount", "{{method}}") in [(s.column, s.row_key) for s in strategies if s.kind == "table_cell"]
