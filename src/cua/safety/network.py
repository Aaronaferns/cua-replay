"""Network containment: the only way production code gets a browser context.

Owner: Agent C. `new_guarded_context` builds a Playwright context that cannot reach anything the `Policy` does not
allow, and reports every refusal as a `BlockedRequest` so it can be logged as evidence.

What is enforced, and where (spike S6, `spikes/s6_escape_hatches.py`, has the probe-by-probe results):

- `context.route("**/*")`: every HTTP(S) request (sub-resources, workers, forms, popups and new tabs in this
  context) is checked against the origin allowlist and path rules; the rest is aborted. Allowed requests are
  fetched by the route itself without following redirects, and a redirect to a refused URL is aborted before the
  browser can follow it (redirect hops are not routed by Playwright; S6 measured the leak).
- `context.route_web_socket`: WebSockets are not seen by `route`; they are checked here and closed when refused.
- `service_workers="block"`: a service worker is a second network stack that `route` does not see.
- `accept_downloads=False` plus a report on every `download` event: nothing is written to disk.
- scheme check: top-level and frame navigations to `file:`, `data:`, `chrome:`, `view-source:` and friends are
  reported, and a page that lands on one is sent back to `about:blank` (or closed when it is a popup).
- `device_scale_factor=1`: screenshots and coordinates are stable.

Limits: a person at a headed browser can still use the address bar and DevTools. Every request they make passes
through the same route (it is the same context), but they are a human with the lease, not automation. Fault
handlers added later with `context.route` run *before* this one; they must call `route.fallback()` (or `fetch()`),
never `route.continue_()`, or they skip the allowlist.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlsplit

from playwright.async_api import Browser, BrowserContext, Download, Frame, Page, Request, Route, WebSocketRoute
from pydantic import BaseModel, ConfigDict

from cua.safety.policy import Policy

# Context options a caller may pass through. Everything that could weaken containment is deliberately absent.
_PASSTHROUGH_OPTIONS = frozenset({"viewport", "locale", "timezone_id", "color_scheme", "storage_state"})
_INERT_URLS = ("about:blank", "about:srcdoc")
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_FETCH_TIMEOUT_MS = 60_000


class BlockedRequest(BaseModel):
    """One refused request or navigation. `url` has no query, fragment or userinfo: it is safe to log."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str
    method: str = "GET"
    resource_type: str = "other"
    navigation: bool = False
    rule: str  # "origin_allowlist" | "path_rules" | "scheme" | "websocket" | "download" | "navigation_scheme"
    reason: str


BlockHandler = Callable[[BlockedRequest], None]


def _loggable(url: str) -> str:
    """Total: never raises. `parts.port` raises ValueError for 'http://h:99999/' or 'http://h:x/', and the callers sit
    in event handlers where an exception would skip the cleanup that follows (`_close_ws`, `_leave`)."""
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme in ("http", "https", "ws", "wss", "file", "ftp"):
            host = parts.hostname or ""
            try:
                port = f":{parts.port}" if parts.port else ""
            except ValueError:
                port = ":invalid-port"
            return f"{scheme}://{host}{port}{parts.path}"[:200]
        return f"{scheme}:" if scheme else "invalid-url"
    except Exception:  # noqa: BLE001 - total by contract
        return "invalid-url"


def new_blocked(
    url: str, *, rule: str, reason: str, method: str = "GET", resource_type: str = "other", navigation: bool = False
) -> BlockedRequest:
    return BlockedRequest(
        url=_loggable(url), method=method, resource_type=resource_type, navigation=navigation, rule=rule, reason=reason
    )


def _navigation_ok(policy: Policy, url: str) -> bool:
    """Is this where a frame may be sitting? about:blank and Chromium's error page are inert; blob: borrows its
    creator's origin; everything else must be an allowed URL."""
    if url in _INERT_URLS or url.startswith("chrome-error://"):
        return True
    if url.startswith("blob:"):
        return policy.check_url(url[len("blob:") :]).allowed
    return policy.check_url(url).allowed


async def new_guarded_context(
    browser: Browser, policy: Policy, *, on_block: BlockHandler, **options: Any
) -> BrowserContext:
    """Create the one kind of browser context production code may use.

    `on_block` is called (synchronously, never raising into the browser) for every refused request, WebSocket,
    download and off-policy navigation. `options` may only be `viewport`, `locale`, `timezone_id`, `color_scheme` or
    `storage_state`; anything else is refused so no caller can reopen an escape hatch.
    """
    unknown = set(options) - _PASSTHROUGH_OPTIONS
    if unknown:
        raise ValueError(f"context option not allowed: {sorted(unknown)}")

    context = await browser.new_context(
        service_workers="block",
        accept_downloads=False,
        device_scale_factor=1,
        ignore_https_errors=False,
        bypass_csp=False,
        java_script_enabled=True,
        **options,
    )
    tasks: set[asyncio.Task[None]] = set()

    def report(blocked: BlockedRequest) -> None:
        with contextlib.suppress(Exception):  # logging must never decide whether traffic is blocked
            on_block(blocked)

    def spawn(coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def on_request(route: Route, request: Request) -> None:
        try:
            decision = policy.check_url(request.url)
            if decision.allowed:
                # `continue_()` would let the browser follow redirects itself, and redirect hops are NOT routed
                # (spike S6: the target answered 302 -> off-allowlist and the evil origin saw the hit). So fetch
                # without following, vet the Location, and hand the browser the redirect only if it is allowed;
                # the browser then follows it as a fresh, routed request.
                try:
                    response = await route.fetch(max_redirects=0, timeout=_FETCH_TIMEOUT_MS)
                except Exception:  # noqa: BLE001 - network failure, not a policy decision
                    with contextlib.suppress(Exception):
                        await route.abort("failed")
                    return
                location = response.headers.get("location")
                if response.status in _REDIRECTS and location:
                    decision = policy.check_url(urljoin(request.url, location))
                    if not decision.allowed:
                        report(
                            new_blocked(
                                urljoin(request.url, location),
                                rule=decision.rule,
                                reason="redirect target: " + decision.reason,
                                method=request.method,
                                resource_type=request.resource_type,
                                navigation=request.is_navigation_request(),
                            )
                        )
                        await route.abort("blockedbyclient")
                        return
                await route.fulfill(response=response)
                return
            report(
                new_blocked(
                    request.url,
                    rule=decision.rule,
                    reason=decision.reason,
                    method=request.method,
                    resource_type=request.resource_type,
                    navigation=request.is_navigation_request(),
                )
            )
        except Exception:  # noqa: BLE001 - anything unexpected is a refusal
            pass
        with contextlib.suppress(Exception):  # the page may already be gone
            await route.abort("blockedbyclient")

    def on_websocket(ws: WebSocketRoute) -> None:
        decision = policy.check_url(ws.url, websocket=True)
        if decision.allowed:
            ws.connect_to_server()
            return
        report(new_blocked(ws.url, rule="websocket", reason=decision.reason, resource_type="websocket"))
        spawn(_close_ws(ws))

    async def _close_ws(ws: WebSocketRoute) -> None:
        with contextlib.suppress(Exception):
            await ws.close(code=1008, reason="blocked by policy")

    def on_download(download: Download) -> None:
        report(new_blocked(download.url, rule="download", reason="downloads are disabled", resource_type="download"))
        spawn(_discard(download))

    async def _discard(download: Download) -> None:
        with contextlib.suppress(Exception):
            await download.cancel()

    def on_frame_navigated(frame: Frame) -> None:
        url = frame.url
        if _navigation_ok(policy, url):
            return
        report(
            new_blocked(
                url,
                rule="navigation_scheme",
                reason="frame is on a URL the policy does not allow",
                resource_type="document",
                navigation=True,
            )
        )
        if frame.parent_frame is None:
            spawn(_leave(frame.page))

    async def _leave(page: Page) -> None:
        with contextlib.suppress(Exception):
            if await page.opener() is not None:
                await page.close()
            else:
                await page.goto("about:blank")

    def watch(page: Page) -> None:
        page.on("framenavigated", on_frame_navigated)
        page.on("download", on_download)

    await context.route("**/*", on_request)
    await context.route_web_socket("**/*", on_websocket)
    context.on("page", watch)
    for page in context.pages:
        watch(page)
    context.on("close", lambda _ctx: [t.cancel() for t in list(tasks)])
    return context
