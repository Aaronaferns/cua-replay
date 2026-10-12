"""Browser fixtures for agent tests: the offline Dolibarr-shaped site from tests/surface, one Chromium per module."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest_asyncio
from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "surface"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakesite import FakeGuard, serve  # noqa: E402

from cua.surface.web import DOLIBARR_SCOPE, WebSurface  # noqa: E402


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def browser():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        yield b
        await b.close()


@pytest_asyncio.fixture(loop_scope="module")
async def context(browser):
    ctx = await browser.new_context(viewport={"width": 1366, "height": 900}, device_scale_factor=1,
                                    service_workers="block", accept_downloads=False)
    await serve(ctx)
    yield ctx
    await ctx.close()


@pytest_asyncio.fixture(loop_scope="module")
async def fake_surface(context):
    """(surface, page, guard) on the fake site's customer list."""
    from fakesite import ORIGIN

    guard = FakeGuard(allow=True)
    page = await context.new_page()
    await page.goto(ORIGIN + "/societe/list.php?type=c")
    return WebSurface(page, guard, scope=DOLIBARR_SCOPE), page, guard
