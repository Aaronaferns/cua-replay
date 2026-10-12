"""Browser fixtures for surface tests: one Chromium per module, a fresh context + fake site per test."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest_asyncio
from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).parent))

from fakesite import ORIGIN, FakeGuard, serve  # noqa: E402

from cua.surface.web import DOLIBARR_SCOPE, WebSurface  # noqa: E402


def context_options() -> dict:
    """Test-only stand-in for `cua.safety.network.new_guarded_context`: same visual options, no network policy."""
    return {"viewport": {"width": 1366, "height": 900}, "device_scale_factor": 1, "service_workers": "block",
            "accept_downloads": False}


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def browser():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        yield b
        await b.close()


@pytest_asyncio.fixture(loop_scope="module")
async def context(browser):
    ctx = await browser.new_context(**context_options())
    await serve(ctx)
    yield ctx
    await ctx.close()


@pytest_asyncio.fixture(loop_scope="module")
async def guard():
    return FakeGuard(allow=True)


@pytest_asyncio.fixture(loop_scope="module")
async def open_page(context, guard):
    """open_page(path) -> (surface, page) with the page loaded and observed once."""

    async def _open(path: str, *, scope=DOLIBARR_SCOPE, guard_=None):
        page = await context.new_page()
        await page.goto(ORIGIN + path)
        surface = WebSurface(page, guard_ or guard, scope=scope)
        return surface, page

    return _open
