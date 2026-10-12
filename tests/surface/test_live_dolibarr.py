"""The M1 acceptance on the real target: a scripted (non-model) driver logs in, opens seeded customers by refs and
reads "Current outstanding bill"; the value equals the REST view of the customer's unpaid invoices."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fakesite import LEASE, FakeGuard, request

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "target" / "dolibarr"))

from dolirest import Rest, customer_by_code, outstanding_of, parse_amount  # noqa: E402
from livetarget import reset_to_baseline  # noqa: E402

from cua.config import settings  # noqa: E402
from cua.surface import snapshot as snap  # noqa: E402
from cua.surface.login import LoginTargets, Target, scripted_login  # noqa: E402
from cua.surface.names import same_name  # noqa: E402
from cua.surface.web import DOLIBARR_SCOPE, WebSurface  # noqa: E402

pytestmark = [pytest.mark.live, pytest.mark.asyncio(loop_scope="module")]

TARGETS = LoginTargets(username=Target(role="textbox", name="Login"), password=Target(role="textbox", name="Password"),
                       submit=Target(role="button", name="Login"))


@pytest.fixture(scope="module")
def baseline():
    return reset_to_baseline()


async def observe_until(surface, ready, *, seconds=15.0):
    """A page change may still be loading right after an action; observe until `ready(obs)` (the classifier's job in
    the real loop, a poll here)."""
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        obs = await surface.observe(screenshot=False)
        if ready(obs) or asyncio.get_running_loop().time() > deadline:
            return obs
        await asyncio.sleep(0.3)


def find_ref(text, role, name):
    for node in snap.parse(text):
        if node.role == role and node.ref and same_name(name, node.name):
            return node.ref
    return None


def ref_of(text, role, name):
    ref = find_ref(text, role, name)
    if ref is None:
        raise AssertionError(f"no {role} named {name!r} in the observation")
    return ref


async def logged_in_surface(context):
    page = await context.new_page()
    surface = WebSurface(page, FakeGuard(allow=True), scope=DOLIBARR_SCOPE)
    base = settings().browser_base_url
    url = f"{base}/index.php"
    await surface.navigate(url, request("navigate", "about:blank", target_url=url))
    await scripted_login(surface, TARGETS, LEASE)
    return surface, page


async def test_scripted_driver_reads_outstanding_bill_that_matches_rest(baseline, browser):
    context = await browser.new_context(viewport={"width": 1366, "height": 900}, device_scale_factor=1,
                                        service_workers="block", accept_downloads=False)
    try:
        surface, page = await logged_in_surface(context)
        admin = Rest.admin(settings())
        seen = []
        for key in ("alpha", "beta"):
            customer = baseline.customer(key)
            list_url = f"{settings().browser_base_url}/societe/list.php?type=c&search_customer_code={customer.code}"
            await surface.navigate(list_url, request("navigate", page.url, target_url=list_url))
            obs = await observe_until(surface, lambda o: customer.name in o.snapshot)
            link = await surface.element_for_ref(ref_of(obs.snapshot, "link", customer.name), obs.epoch)
            await surface.click(link, request("click", page.url))

            # the name link opens the third-party card; the outstanding amounts are on its "Customer" tab
            obs = await observe_until(surface, lambda o: find_ref(o.snapshot, "link", "Customer") is not None)
            tab = await surface.element_for_ref(ref_of(obs.snapshot, "link", "Customer"), obs.epoch)
            await surface.click(tab, request("click", page.url))

            obs = await observe_until(
                surface, lambda o: find_ref(o.snapshot, "generic", "Current outstanding bill") is not None)
            box = await surface.element_for_ref(ref_of(obs.snapshot, "generic", "Current outstanding bill"), obs.epoch)
            shown = parse_amount(await surface.read_text(box))

            socid = customer_by_code(admin, customer.code)["id"]
            assert shown == outstanding_of(admin, socid) == customer.outstanding
            seen.append(shown)
        assert len(set(seen)) == 2  # two different customers, two different amounts
    finally:
        await context.close()


async def test_near_miss_filter_finds_only_the_longer_code(baseline, browser):
    """Typing a code that does not exist but prefixes another one shows only that one row (the spike's near miss)."""
    context = await browser.new_context(viewport={"width": 1366, "height": 900}, device_scale_factor=1,
                                        service_workers="block", accept_downloads=False)
    try:
        surface, page = await logged_in_surface(context)
        url = f"{settings().browser_base_url}/societe/list.php?type=c"
        await surface.navigate(url, request("navigate", page.url, target_url=url))
        from cua.artifact.schema import Strategy

        box = await surface.find(Strategy(kind="scoped", role="textbox", within_role="columnheader",
                                          within_name="Customer Code", why="test", matched_at_record="test"))
        assert len(box) == 1
        await surface.type(box[0], baseline.near_miss_missing_code, request("type", page.url))
        await surface.press("Enter", request("press", page.url))
        obs = await observe_until(surface, lambda o: baseline.near_miss_existing_code in o.snapshot)
        assert baseline.near_miss_existing_code in obs.snapshot
        assert baseline.customer("alpha").code not in obs.snapshot
    finally:
        await context.close()
