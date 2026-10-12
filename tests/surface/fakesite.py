"""An offline stand-in for the Dolibarr target: fixture pages served through Playwright routes at a fake origin, so
navigation, links and form posts behave like a real site with no network and no Docker."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, Route

from cua.artifact.schema import Effect
from cua.control.lease import LeaseToken
from cua.safety.policy import ActionKind, ActionRequest, PolicyDecision

FIXTURES = Path(__file__).parent / "fixtures"
ORIGIN = "http://dolibarr.test"
PAGES = {
    "/comm/card.php": "customer_card.html",
    "/societe/list.php": "customer_list.html",
    "/index.php": "login.html",
}
LEASE = LeaseToken(holder="automation", generation=1)


async def serve(context: BrowserContext, pages: dict[str, str] = PAGES) -> None:
    async def deny(route: Route) -> None:
        await route.abort()

    async def page(route: Route) -> None:
        parts = urlsplit(route.request.url)
        name = pages.get(parts.path)
        if parts.path == "/index.php" and "captcha" in parts.query:
            name = "login_captcha.html"
        elif parts.path == "/index.php" and route.request.method == "POST":
            # the fake login accepts exactly one account, and only the test knows its password
            body = route.request.post_data or ""
            if "username=tester&" in body and "password=pw-for-test" in body:
                name = "customer_list.html"
        if name is None:
            await route.fulfill(status=404, content_type="text/html", body="<title>Not found</title>Not found")
        else:
            await route.fulfill(status=200, content_type="text/html; charset=utf-8",
                                body=(FIXTURES / name).read_text(encoding="utf-8"))

    await context.route("**/*", deny)  # nothing leaves the test
    await context.route(f"{ORIGIN}/**", page)  # registered last, so it is matched first


class FakeGuard:
    """Implements the `Guard` protocol for tests: allows or denies everything and remembers what it was asked."""

    def __init__(self, allow: bool = True) -> None:
        self.allow = allow
        self.calls: list[ActionRequest] = []

    def check(self, request: ActionRequest) -> PolicyDecision:
        self.calls.append(request)
        if self.allow:
            return PolicyDecision(allowed=True, rule="test_allow", reason="test guard allows")
        return PolicyDecision(allowed=False, rule="test_deny", reason="test guard denies", approvable=False)


def request(kind: ActionKind, page_url: str, *, target_url: str | None = None, effect: Effect = "none",
            actor: str = "agent") -> ActionRequest:
    return ActionRequest(kind=kind, actor=actor, page_url=page_url, target_url=target_url, effect=effect,
                         lease=LEASE)
