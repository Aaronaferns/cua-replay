"""Network containment against real Chromium and two local servers. The denied server's own hit log is the oracle:
a request that reached it escaped, whatever Playwright reported."""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from playwright.async_api import Browser, BrowserContext, Page

from cua.safety.network import BlockedRequest, new_guarded_context
from cua.safety.policy import Policy


def policy_for(*origins: str, deny: list[str] | None = None) -> Policy:
    return Policy.model_validate({
        "version": "cua.policy/v1",
        "name": "net-test",
        "sandbox": True,
        "origins": list(origins),
        "routes": {"allow": ["/**"], "deny": deny or []},
        "actions": ["click", "navigate", "extract"],
        "max_effect": "read",
    })


@pytest.fixture
async def world(browser: Browser, servers):
    allowed, denied = servers(), servers()
    blocked: list[BlockedRequest] = []
    context = await new_guarded_context(browser, policy_for(allowed.origin, deny=["/secret/**"]), on_block=blocked.append)
    yield allowed, denied, context, blocked
    with contextlib.suppress(Exception):
        await context.close()


async def settle(page: Page | None = None, ms: int = 600) -> None:
    await asyncio.sleep(ms / 1000)


async def test_network_blocks_off_allowlist(world):
    allowed, denied, context, blocked = world
    allowed.serve("/", f"""<h1>app</h1><img src="{denied.origin}/img.png"><script src="{denied.origin}/x.js"></script>
        <iframe src="{denied.origin}/frame"></iframe><a id=out target=_blank href="{denied.origin}/blank">out</a>""")
    page = await context.new_page()
    await page.goto(allowed.origin + "/")
    await page.evaluate(f"fetch('{denied.origin}/fetch').catch(() => {{}}); navigator.sendBeacon('{denied.origin}/b', 'x')")
    await page.click("#out")
    await page.evaluate(f"window.open('{denied.origin}/popup')")
    await settle()

    assert await page.inner_text("h1") == "app"  # the allowed origin works
    assert denied.hits == []  # nothing reached the denied origin
    assert {b.rule for b in blocked} == {"origin_allowlist"}
    urls = {b.url for b in blocked}
    assert {f"{denied.origin}/img.png", f"{denied.origin}/fetch", f"{denied.origin}/popup"} <= urls
    assert all(b.url.startswith(denied.origin) for b in blocked)


async def test_blocked_requests_are_reported_without_query_or_userinfo(world):
    allowed, denied, context, blocked = world
    page = await context.new_page()
    await page.goto(allowed.origin + "/")
    await page.evaluate(f"fetch('{denied.origin}/leak?token=abcd1234&customer=CU1910-00021').catch(() => {{}})")
    await settle()
    assert denied.hits == []
    assert [b.url for b in blocked] == [f"{denied.origin}/leak"]
    assert "abcd1234" not in blocked[0].model_dump_json() and "CU1910" not in blocked[0].model_dump_json()


async def test_path_rules_apply_on_the_allowed_origin(world):
    allowed, denied, context, blocked = world
    page = await context.new_page()
    await page.goto(allowed.origin + "/ok")
    result = await page.evaluate("fetch('/secret/data').then(r => r.status).catch(() => 'blocked')")
    assert result == "blocked"
    assert [h for h in allowed.hits if "/secret" in h] == []
    assert [b.rule for b in blocked] == ["path_rules"]


async def test_redirect_to_a_denied_origin_is_stopped_before_the_browser_follows(world):
    allowed, denied, context, blocked = world
    page = await context.new_page()
    with pytest.raises(Exception):  # noqa: B017, PT011 - navigation aborted
        await page.goto(f"{allowed.origin}/redirect?to={denied.origin}/landed")
    second = await context.new_page()
    await second.goto(allowed.origin + "/")
    await second.evaluate(f"setTimeout(() => location.href = '{allowed.origin}/redirect?to={denied.origin}/again', 0)")
    await settle()
    assert denied.hits == []
    assert any(b.reason.startswith("redirect target") for b in blocked)


async def test_redirect_within_the_allowlist_still_works(world):
    allowed, denied, context, blocked = world
    page = await context.new_page()
    await page.goto(f"{allowed.origin}/redirect?to={allowed.origin}/landed")
    assert page.url == f"{allowed.origin}/landed"
    assert blocked == []


async def test_redirect_to_a_denied_path_is_stopped(world):
    allowed, denied, context, blocked = world
    page = await context.new_page()
    with pytest.raises(Exception):  # noqa: B017, PT011
        await page.goto(f"{allowed.origin}/redirect?to=/secret/area")
    assert "GET /secret/area" not in allowed.hits  # the redirect was served, its target never requested
    assert [b.rule for b in blocked] == ["path_rules"]


async def test_cookies_and_form_posts_survive_the_guard(world):
    allowed, denied, context, blocked = world
    allowed.serve("/login", "<h1>in</h1>", headers={"Set-Cookie": "sid=abc123; Path=/"})
    page = await context.new_page()
    await page.goto(allowed.origin + "/login")
    assert "sid=abc123" in await page.evaluate("document.cookie")
    await page.evaluate("""() => { const f = document.createElement('form'); f.method = 'POST'; f.action = '/submit';
        document.body.appendChild(f); f.submit(); }""")
    await page.wait_for_url("**/submit")
    assert "POST /submit" in allowed.hits
    assert blocked == []


async def test_websocket_to_a_denied_origin_is_never_opened(world):
    allowed, denied, context, blocked = world
    page = await context.new_page()
    await page.goto(allowed.origin + "/")
    ws_url = denied.origin.replace("http://", "ws://") + "/ws"
    await page.evaluate(f"""() => {{ window.state = 'pending'; const ws = new WebSocket('{ws_url}');
        ws.onopen = () => window.state = 'open'; ws.onclose = () => window.state = 'closed';
        ws.onerror = () => window.state = 'error'; }}""")
    await settle(ms=800)
    assert denied.hits == []
    assert await page.evaluate("window.state") != "open"
    assert [b.rule for b in blocked] == ["websocket"]


async def test_downloads_are_refused_and_nothing_is_written(world):
    allowed, denied, context, blocked = world
    allowed.serve("/export.csv", "a,b\n1,2\n", ctype="text/csv",
                  headers={"Content-Disposition": 'attachment; filename="export.csv"'})
    allowed.serve("/", "<a id=dl href='/export.csv'>dl</a>")
    page = await context.new_page()
    saved: list[str] = []

    async def on_download(d):
        with contextlib.suppress(Exception):
            if p := await d.path():
                saved.append(str(p))

    page.on("download", lambda d: asyncio.ensure_future(on_download(d)))
    await page.goto(allowed.origin + "/")
    await page.click("#dl")
    await settle()
    assert saved == []
    assert [b.rule for b in blocked] == ["download"]


@pytest.mark.parametrize("scheme_url", ["file:///C:/Windows/win.ini", "file:///etc/passwd"])
async def test_file_navigation_is_refused(world, scheme_url):
    allowed, denied, context, blocked = world
    page = await context.new_page()
    await page.goto(allowed.origin + "/")
    with pytest.raises(Exception):  # noqa: B017, PT011
        await page.goto(scheme_url)
    assert any(b.rule in {"scheme", "navigation_scheme"} for b in blocked)
    assert not page.url.startswith("file:")


async def test_data_url_page_is_left_and_its_subresources_are_blocked(world):
    allowed, denied, context, blocked = world
    page = await context.new_page()
    await page.goto(allowed.origin + "/")
    await page.goto(f"data:text/html,<h1>d</h1><img src='{denied.origin}/from-data'>")
    await settle(ms=800)
    assert denied.hits == []
    assert page.url == "about:blank"
    assert any(b.rule == "navigation_scheme" for b in blocked)


async def test_service_workers_are_blocked(world):
    allowed, denied, context, blocked = world
    allowed.serve("/sw.js", "self.addEventListener('fetch', () => {});", ctype="application/javascript")
    page = await context.new_page()
    await page.goto(allowed.origin + "/")
    outcome = await page.evaluate("""() => navigator.serviceWorker.register('/sw.js')
        .then(() => Promise.race([navigator.serviceWorker.ready.then(() => 'active'),
                                  new Promise(r => setTimeout(() => r('never active'), 1500))]))
        .catch(e => 'rejected')""")
    assert outcome != "active"


async def test_device_scale_factor_is_one(world):
    allowed, denied, context, blocked = world
    page = await context.new_page()
    await page.goto(allowed.origin + "/")
    assert await page.evaluate("window.devicePixelRatio") == 1


async def test_context_options_that_weaken_containment_are_refused(browser: Browser):
    policy = policy_for("http://127.0.0.1:1")
    for bad in ({"accept_downloads": True}, {"service_workers": "allow"}, {"bypass_csp": True},
                {"ignore_https_errors": True}, {"proxy": {"server": "http://x:1"}}):
        with pytest.raises(ValueError):
            await new_guarded_context(browser, policy, on_block=lambda b: None, **bad)
    context = await new_guarded_context(browser, policy, on_block=lambda b: None, viewport={"width": 800, "height": 600})
    await context.close()


async def test_a_failing_block_callback_never_unblocks(browser: Browser, servers):
    allowed, denied = servers(), servers()

    def explode(_: BlockedRequest) -> None:
        raise RuntimeError("log sink down")

    context = await new_guarded_context(browser, policy_for(allowed.origin), on_block=explode)
    page: Page = await context.new_page()
    await page.goto(allowed.origin + "/")
    await page.evaluate(f"fetch('{denied.origin}/x').catch(() => {{}})")
    await settle()
    assert denied.hits == []
    await context.close()


async def test_new_tabs_in_the_context_are_guarded_too(world):
    allowed, denied, context, blocked = world
    first = await context.new_page()
    await first.goto(allowed.origin + "/")
    second: BrowserContext | Page = await context.new_page()  # what a user-opened tab amounts to inside the context
    with pytest.raises(Exception):  # noqa: B017, PT011
        await second.goto(denied.origin + "/typed-in-the-address-bar")
    assert denied.hits == []
