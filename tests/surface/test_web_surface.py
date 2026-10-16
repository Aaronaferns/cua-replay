"""WebSurface on offline Dolibarr-shaped pages: scoped observation, refs, finding, guarded actions."""

from __future__ import annotations

import time

import pytest
from fakesite import ORIGIN, FakeGuard, request

from cua.artifact.schema import Strategy
from cua.safety.policy import PolicyDenied
from cua.surface import snapshot as snap
from cua.surface.base import NativeDialogOpen, StaleRefError, UnknownRefError
from cua.surface.web import WebScope

pytestmark = pytest.mark.asyncio(loop_scope="module")

FILTER_NAME = "For searching inside text fields, you can use the characters ^ or $ to make a 'start or end with' search."


def ref_of(text: str, role: str, name: str | None = None) -> str:
    for node in snap.parse(text):
        if node.role == role and (name is None or node.name == name) and node.ref:
            return node.ref
    raise AssertionError(f"no {role} {name!r} in snapshot")


def strategy(**kw) -> Strategy:
    return Strategy(why="test", matched_at_record="test", **kw)


async def test_observe_scopes_to_main_content(open_page):
    surface, page = await open_page("/comm/card.php?socid=10")
    full = await page.aria_snapshot(mode="ai")
    obs = await surface.observe()

    assert obs.epoch == 1
    assert "Customer Code" in obs.snapshot and "Current outstanding bill" in obs.snapshot
    # menus are not in the scoped snapshot ...
    assert "New third party" not in obs.snapshot and "Billing | Payment" not in obs.snapshot
    assert len(obs.snapshot) < len(full)
    # ... they are summarized as link names only, without user data
    assert {"Home", "Third parties", "Billing | Payment", "Customers", "New third party"} <= set(obs.menu_summary)
    assert not any("@" in name for name in obs.menu_summary)
    assert obs.facts.title == "Northwind Test - Customer" and obs.facts.dialog is None
    # a plain 1x viewport screenshot
    assert obs.screenshot_png is not None and obs.screenshot_png[:4] == b"\x89PNG"
    width = int.from_bytes(obs.screenshot_png[16:20], "big")
    assert width == 1366


async def test_observe_masks_csrf_tokens(open_page):
    surface, _ = await open_page("/comm/card.php?socid=10")
    obs = await surface.observe(screenshot=False)
    assert "token=***" in obs.snapshot
    assert "0123456789abcdef0123456789abcdef" not in obs.snapshot


async def test_observe_whole_page_when_no_main_container(open_page):
    surface, _ = await open_page("/index.php")
    obs = await surface.observe(screenshot=False)
    assert 'textbox "Login"' in obs.snapshot and 'textbox "Password"' in obs.snapshot
    assert obs.menu_summary == []


async def test_scoped_snapshot_refs_resolve_with_aria_ref(open_page):
    """Refs from a locator-scoped AI snapshot resolve with page.locator('aria-ref=...') (Playwright 1.63)."""
    surface, _ = await open_page("/comm/card.php?socid=10")
    obs = await surface.observe(screenshot=False)
    refs = snap.refs(obs.snapshot)
    assert len(refs) > 20
    for ref in refs:
        element = await surface.element_for_ref(ref, obs.epoch)
        assert await element.locator.count() == 1


async def test_dialog_layer_is_observed_separately(open_page):
    surface, page = await open_page("/comm/card.php?socid=10")
    obs = await surface.observe(screenshot=False)
    await surface.click(await surface.element_for_ref(ref_of(obs.snapshot, "button", "Open dialog"), obs.epoch),
                        request("click", page.url))
    obs = await surface.observe(screenshot=False)
    assert obs.facts.dialog == "Validate invoice"
    assert obs.dialog_snapshot is not None and 'button "Yes"' in obs.dialog_snapshot
    assert "Are you sure" not in obs.snapshot
    yes = await surface.element_for_ref(ref_of(obs.dialog_snapshot, "button", "Yes"), obs.epoch)
    assert surface.accessible(yes) == ("button", "Yes")
    # the dialog is modal: the main content is shown for context, without actionable refs
    assert "Customer Code" in obs.snapshot and snap.refs(obs.snapshot) == []
    with pytest.raises(UnknownRefError):
        await surface.element_for_ref("e7", obs.epoch)


async def test_elements_survive_later_snapshots_but_refs_do_not(open_page):
    """Playwright keeps only the latest snapshot's refs: a resolved element must not depend on them, and a find()
    (which snapshots to verify names) invalidates the refs of the observation instead of resolving the wrong one."""
    surface, page = await open_page("/comm/card.php?socid=10")
    obs = await surface.observe(screenshot=False)
    back = await surface.element_for_ref(ref_of(obs.snapshot, "link", "Back to list"), obs.epoch)
    other = ref_of(obs.snapshot, "button", "Open dialog")
    found = await surface.find(strategy(kind="role", role="link", name="Customer"))
    assert len(found) == 1
    with pytest.raises(StaleRefError):
        await surface.element_for_ref(other, obs.epoch)
    assert surface.accessible(back) == ("link", "Back to list")
    assert (await surface.fingerprint(back)).role == "link"
    await surface.click(back, request("click", page.url))
    assert (await surface.observe(screenshot=False)).facts.title == "Customers"


async def test_stale_ref_from_older_epoch_is_rejected(open_page):
    surface, _ = await open_page("/comm/card.php?socid=10")
    old = await surface.observe(screenshot=False)
    ref = ref_of(old.snapshot, "link", "Back to list")
    await surface.observe(screenshot=False)
    with pytest.raises(StaleRefError):
        await surface.element_for_ref(ref, old.epoch)


async def test_stale_ref_fails_fast_when_element_is_gone(open_page):
    surface, page = await open_page("/comm/card.php?socid=10")
    obs = await surface.observe(screenshot=False)
    ref = ref_of(obs.snapshot, "link", "Back to list")
    await page.evaluate("document.querySelector('.fiche > a').remove()")  # the page changed under us
    started = time.perf_counter()
    with pytest.raises(StaleRefError):
        await surface.element_for_ref(ref, obs.epoch)
    assert time.perf_counter() - started < 1.0  # a typed error, not a 30 s auto-wait


async def test_ref_outside_the_observed_scope_is_rejected(open_page):
    surface, page = await open_page("/comm/card.php?socid=10")
    obs = await surface.observe(screenshot=False)
    menu_ref = ref_of(await page.aria_snapshot(mode="ai"), "link", "New third party")
    assert menu_ref not in obs.snapshot
    with pytest.raises(UnknownRefError):
        await surface.element_for_ref(menu_ref, obs.epoch)


async def test_find_role_name_is_normalized_exact(open_page):
    surface, _ = await open_page("/comm/card.php?socid=10")
    found = await surface.find(strategy(kind="role", role="generic", name="Current outstanding bill"))
    assert len(found) == 1
    assert await surface.read_text(found[0]) == "Current outstanding bill 75.37 €"
    assert len(await surface.find(strategy(kind="role", role="generic", name="current outstanding bill:"))) == 1
    assert len(await surface.find(strategy(kind="role", role="generic", name="Current outstanding bill late"))) == 1
    assert await surface.find(strategy(kind="role", role="generic", name="outstanding bill")) == []
    assert len(await surface.find(strategy(kind="role", role="cell", name="Customer Code *"))) == 1
    assert await surface.find(strategy(kind="role", role="cell", name="Code")) == []


async def test_find_same_row_value_cell(open_page):
    surface, _ = await open_page("/comm/card.php?socid=10")
    found = await surface.find(strategy(kind="same_row", label="Customer Code", take="value"))
    assert len(found) == 1
    assert await surface.read_text(found[0]) == "CU1212-0005"  # NBSP folded


async def test_shared_name_filters_are_ambiguous_until_scoped_by_column(open_page):
    """Six filter textboxes share one long help-text name: role+name never picks 'the first' of them."""
    surface, _ = await open_page("/societe/list.php")
    by_name = await surface.find(strategy(kind="role", role="textbox", name=FILTER_NAME))
    assert len(by_name) == 6
    scoped = await surface.find(strategy(kind="scoped", role="textbox", within_role="columnheader",
                                         within_name="Customer Code"))
    assert len(scoped) == 1
    fp = await surface.fingerprint(scoped[0])
    assert (fp.role, fp.tag, fp.input_type, fp.column_header, fp.row_label) == (
        "textbox", "input", "text", "Customer Code", None)
    assert sum([await surface.same_element(scoped[0], e) for e in by_name]) == 1


async def test_find_table_cell_by_row_key_and_column(open_page):
    surface, _ = await open_page("/societe/list.php")
    found = await surface.find(strategy(kind="table_cell", row_key="CU1910-00021", column="Third-party name"))
    assert len(found) == 1
    assert await surface.read_text(found[0]) == "Company Corp 2"
    assert await surface.find(strategy(kind="table_cell", row_key="CU1910-0002", column="Third-party name")) == []


async def test_fingerprint_row_label_in_nested_layout_table(open_page):
    surface, _ = await open_page("/comm/card.php?socid=10")
    obs = await surface.observe(screenshot=False)
    value = await surface.element_for_ref(ref_of(obs.snapshot, "cell", "411CU12120005"), obs.epoch)
    fp = await surface.fingerprint(value)
    assert (fp.role, fp.tag, fp.row_label, fp.column_header) == ("cell", "td", "Customer accounting code", None)
    code = (await surface.find(strategy(kind="same_row", label="Customer Code", take="value")))[0]
    assert (await surface.fingerprint(code)).row_label == "Customer Code"


async def test_same_element_compares_identity(open_page):
    surface, _ = await open_page("/comm/card.php?socid=10")
    obs = await surface.observe(screenshot=False)
    by_ref = await surface.element_for_ref(ref_of(obs.snapshot, "cell", "CU1212-0005"), obs.epoch)
    by_row = (await surface.find(strategy(kind="same_row", label="Customer Code", take="value")))[0]
    other = (await surface.find(strategy(kind="role", role="cell", name="Customer Code")))[0]
    assert await surface.same_element(by_ref, by_row)
    assert not await surface.same_element(by_ref, other)


async def test_guard_denies_before_ui_is_touched(open_page):
    deny = FakeGuard(allow=False)
    surface, page = await open_page("/societe/list.php", guard_=deny)
    box = (await surface.find(strategy(kind="scoped", role="textbox", within_role="columnheader",
                                       within_name="Customer Code")))[0]
    with pytest.raises(PolicyDenied):
        await surface.type(box, "CU1910-0002", request("type", page.url))
    assert await box.locator.input_value() == ""
    link = (await surface.find(strategy(kind="role", role="link", name="Company Corp 2")))[0]
    with pytest.raises(PolicyDenied):
        await surface.click(link, request("click", page.url))
    url = ORIGIN + "/comm/card.php?socid=21"
    with pytest.raises(PolicyDenied):
        await surface.navigate(url, request("navigate", page.url, target_url=url))
    with pytest.raises(PolicyDenied):
        await surface.press("Enter", request("press", page.url))
    assert page.url == ORIGIN + "/societe/list.php"
    assert [r.kind for r in deny.calls] == ["type", "click", "navigate", "press"]


async def test_allowed_action_returns_the_decision_and_type_clears_first(open_page, guard):
    surface, page = await open_page("/societe/list.php")
    box = (await surface.find(strategy(kind="scoped", role="textbox", within_role="columnheader",
                                       within_name="Customer Code")))[0]
    await surface.type(box, "stale text", request("type", page.url))
    decision = await surface.type(box, "CU1910-0002", request("type", page.url))
    assert decision.allowed and decision.rule == "test_allow"
    assert await box.locator.input_value() == "CU1910-0002"
    assert len(guard.calls) == 2  # consulted exactly once per action


async def test_inputs_rejected_before_the_guard(open_page, guard):
    surface, page = await open_page("/societe/list.php")
    box = (await surface.find(strategy(kind="role", role="textbox", name=FILTER_NAME)))[0]
    with pytest.raises(ValueError):
        await surface.type(box, "CU1910\n0002", request("type", page.url))
    with pytest.raises(ValueError):
        await surface.press("Control+W", request("press", page.url))
    with pytest.raises(ValueError):
        await surface.click(box, request("type", page.url))  # request kind must match the action
    url = ORIGIN + "/comm/card.php?socid=21"
    with pytest.raises(ValueError):
        await surface.navigate(url, request("navigate", page.url, target_url=ORIGIN + "/elsewhere"))
    assert guard.calls == []


async def test_navigate_and_link_click_follow_the_site(open_page):
    surface, page = await open_page("/societe/list.php")
    link = (await surface.find(strategy(kind="role", role="link", name="Northwind Test")))[0]
    await surface.click(link, request("click", page.url))
    obs = await surface.observe(screenshot=False)
    assert obs.facts.url == ORIGIN + "/comm/card.php?socid=10" and "Customer Code" in obs.snapshot
    url = ORIGIN + "/societe/list.php"
    await surface.navigate(url, request("navigate", page.url, target_url=url))
    assert (await surface.observe(screenshot=False)).facts.title == "Customers"


async def test_native_dialog_is_held_never_accepted(open_page):
    surface, page = await open_page("/comm/card.php?socid=10")
    button = (await surface.find(strategy(kind="role", role="button", name="Fire alert")))[0]
    started = time.perf_counter()
    with pytest.raises(NativeDialogOpen):
        await surface.click(button, request("click", page.url))
    assert time.perf_counter() - started < 3.0
    obs = await surface.observe()
    assert obs.facts.native_dialog == "alert: Session will expire"
    assert obs.snapshot == "" and obs.screenshot_png is None
    await surface.dismiss_native_dialog(request("click", page.url))
    assert "Customer Code" in (await surface.observe(screenshot=False)).snapshot


async def test_masked_screenshot_hides_masked_elements(open_page):
    surface, _ = await open_page("/comm/card.php?socid=10")
    obs = await surface.observe()
    code = (await surface.find(strategy(kind="same_row", label="Customer Code", take="value")))[0]
    masked = await surface.masked_screenshot([code], generic=False)
    generic = await surface.masked_screenshot([], generic=True)
    assert masked[:4] == b"\x89PNG" and masked != obs.screenshot_png and generic != masked


async def test_scope_is_configurable(open_page):
    surface, _ = await open_page("/comm/card.php?socid=10", scope=WebScope(main=".tabs", menus=()))
    obs = await surface.observe(screenshot=False)
    assert 'link "Customer"' in obs.snapshot and "Customer Code" not in obs.snapshot
    assert surface.describe()["scope"]["main"] == ".tabs"


async def test_an_abandoned_page_operation_does_not_leak_an_unretrieved_exception():
    # a step timeout cancels the surface call; the operation's late failure must be read, not logged as "never retrieved"
    import asyncio
    import gc

    from cua.surface.web import WebSurface

    async def late_failure():
        await asyncio.sleep(0.05)
        raise RuntimeError("net::ERR_ABORTED")

    surface = WebSurface.__new__(WebSurface)
    surface._native, surface._native_event = None, asyncio.Event()
    leaked = []
    asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: leaked.append(ctx))
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(surface._race_native(late_failure()), 0.01)
    await asyncio.sleep(0.1)
    gc.collect()
    await asyncio.sleep(0)
    assert leaked == []
