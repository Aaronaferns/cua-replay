"""`table_cell` finds the `role` element inside the cell, in the row keyed by a value (or in every row)."""

from __future__ import annotations

import pytest

from cua.artifact.schema import Strategy

pytestmark = pytest.mark.asyncio(loop_scope="module")


def cell(**kw) -> Strategy:
    return Strategy(kind="table_cell", why="t", matched_at_record="t", **kw)


async def test_link_in_the_keyed_row_and_column(open_page):
    surface, page = await open_page("/societe/list.php?type=c")
    found = await surface.find(cell(column="Third-party name", row_key="CU1212-0005", role="link"))
    assert len(found) == 1 and surface.accessible(found[0]) == ("link", "Northwind Test")


async def test_without_a_row_key_every_row_matches(open_page):
    surface, page = await open_page("/societe/list.php?type=c")
    names = sorted(surface.accessible(e)[1] for e in await surface.find(cell(column="Third-party name", role="link")))
    assert names == ["Company Corp 2", "Contoso Test", "Northwind Test"]


async def test_cell_itself_without_a_role(open_page):
    surface, page = await open_page("/societe/list.php?type=c")
    found = await surface.find(cell(column="Customer Code", row_key="CU1212-0007"))
    assert len(found) == 1 and await surface.read_text(found[0]) == "CU1212-0007"
