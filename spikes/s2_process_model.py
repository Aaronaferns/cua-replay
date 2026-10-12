"""Spike S2: one `asyncio.run()` hosts uvicorn and a (headed) Playwright browser on Dolibarr.

Questions, each answered by a check below (`PASS`/`FAIL` lines, exit code 1 if any fails):

  pause/resume  an `asyncio.Event` gates an automation loop; `POST /pause` and `POST /resume` flip it over HTTP
  capture       `context.expose_binding` + `context.add_init_script` report click, change, submit and Enter across
                full-page navigations and POSTs; values are replaced by `[REDACTED]` inside the page, so a typed
                sentinel never reaches Python
  request log   `context.on("request")` logs human `action=confirm_*` GETs (token never logged)
  shutdown      SIGINT (as Ctrl+C delivers it to `asyncio.run`) or task cancellation ends everything: server stopped,
                browser and Playwright driver gone, no traceback

The "human" is simulated with Playwright input on the page (so `isTrusted` events, but no person); automation and
human phases are told apart by a flag on our side, as the lease will do in M5. The browser context comes from
`open_session` -> `new_guarded_context`, so the capture runs on the real containment layer.

    uv run python spikes/s2_process_model.py                       # real Dolibarr target, headed
    uv run python spikes/s2_process_model.py --headless
    uv run python spikes/s2_process_model.py --target local        # self-contained fake site, no Docker
    uv run python spikes/s2_process_model.py --interrupt cancel     # task cancellation instead of SIGINT
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "target" / "dolibarr"))

import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from cua.config import Settings, settings  # noqa: E402
from cua.control.runtime import open_session  # noqa: E402
from cua.safety.network import loggable_url  # noqa: E402

SENTINEL = "HUMAN-TYPED-7431"
CONFIRM = re.compile(r"[?&]action=(confirm_[a-z_]+)")

CAPTURE_JS = r"""
(() => {
  if (window.__cuaCaptureInstalled) return;
  window.__cuaCaptureInstalled = true;
  const label = el => ((el.labels && el.labels[0] && el.labels[0].innerText) || el.getAttribute('aria-label')
      || el.getAttribute('placeholder') || '').slice(0, 40);
  const base = (kind, el) => ({kind, tag: (el.tagName || '').toLowerCase(), type: el.type || null,
      name: el.name || el.id || null, label: label(el), path: location.pathname, t: Date.now()});
  const send = o => { try { window.__cuaCapture(JSON.stringify(o)); } catch (e) {} };
  document.addEventListener('click', e => {
    const el = e.target.closest('a,button,input,select,textarea,[role=button]') || e.target;
    const o = base('click', el);
    if (el.tagName === 'A' || el.tagName === 'BUTTON') o.text = (el.innerText || '').trim().slice(0, 40);
    send(o);
  }, true);
  document.addEventListener('change', e => {
    const o = base('change', e.target);
    o.value = '[REDACTED]';
    o.length = (e.target.value || '').length;
    send(o);
  }, true);
  document.addEventListener('keydown', e => {
    if (e.key === 'Enter' && /^(INPUT|SELECT)$/.test(e.target.tagName)) send(base('enter', e.target));
  }, true);
  document.addEventListener('submit', e => {
    const f = e.target, o = base('submit', f);
    o.method = (f.method || 'get').toLowerCase();
    o.action = new URL(f.action, location.href).pathname;
    o.fields = Array.from(f.elements).filter(x => x.name).map(x => x.name);
    o.value = '[REDACTED]';
    send(o);
  }, true);
})();
"""


# ----------------------------------------------------------------------------------------- the fake target (local)

PAGES = {
    "login": """<!DOCTYPE html><html><head><title>Login</title></head><body><form method="POST" action="/index.php">
<div title="Enter login details"><input type="text" name="username" aria-label="Login">
<input type="password" name="password" aria-label="Password"></div><input type="submit" value="Login"></form></body></html>""",
    "home": '<!DOCTYPE html><html><head><title>Home</title></head><body><a href="/societe/list.php">Third parties</a></body></html>',
    "list": """<!DOCTYPE html><html><head><title>List</title></head><body><form method="GET" action="/societe/list.php">
<input type="text" name="search_name" aria-label="Name filter"><select name="limit" aria-label="Limit">
<option value="10">10</option><option value="25">25</option></select><input type="submit" value="Search"></form>
<a href="/comm/card.php?socid=1">Aurora Seed Supplies</a></body></html>""",
    "card": """<!DOCTYPE html><html><head><title>Card</title></head><body><form method="POST" action="/comm/card.php?socid=1">
<input type="text" name="note" aria-label="Note"><input type="submit" value="Save"></form>
<a href="/compta/facture/card.php?facid=1&amp;action=confirm_validate&amp;confirm=yes&amp;token=SPIKE-TOKEN-1234">Validate</a>
</body></html>""",
    "invoice": "<!DOCTYPE html><html><head><title>Invoice</title></head><body>Invoice</body></html>",
}
LOCAL_PASSWORD = "pw-for-spike-s2"


class FakeTarget:
    def __init__(self) -> None:
        pages = PAGES

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: D102
                pass

            def _send(self, html: str) -> None:
                data = html.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                path = self.path.split("?")[0]
                key = {"/societe/list.php": "list", "/comm/card.php": "card", "/compta/facture/card.php": "invoice"}
                self._send(pages[key.get(path, "login")])

            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
                if self.path.startswith("/index.php"):
                    self._send(pages["home"] if f"password={LOCAL_PASSWORD}" in body else pages["login"])
                else:
                    self._send(pages["card"])

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# ----------------------------------------------------------------------------------------------------- the control

class Control:
    """State shared by the HTTP endpoints, the automation loop and the browser callbacks."""

    def __init__(self) -> None:
        self.running = asyncio.Event()
        self.running.set()
        self.phase = "automation"  # who holds the lease: capture only counts while a human does
        self.steps = 0
        self.captured: list[dict] = []
        self.confirms: list[dict] = []

    async def on_capture(self, source, payload: str) -> None:
        if self.phase == "human":
            self.captured.append(json.loads(payload))

    def on_request(self, request) -> None:
        match = CONFIRM.search(request.url)
        if request.method == "GET" and match:
            self.confirms.append({"action": match.group(1), "url": loggable_url(request.url), "phase": self.phase,
                                  "token_logged": "token" in loggable_url(request.url)})


def build_app(control: Control) -> FastAPI:
    app = FastAPI()

    @app.get("/status")
    async def status() -> dict:
        return {"running": control.running.is_set(), "steps": control.steps, "phase": control.phase,
                "captured": len(control.captured), "confirms": len(control.confirms)}

    @app.post("/pause")
    async def pause() -> dict:
        control.running.clear()
        return {"running": False}

    @app.post("/resume")
    async def resume() -> dict:
        control.running.set()
        return {"running": True}

    return app


class NoSignalServer(uvicorn.Server):
    """uvicorn would install its own SIGINT/SIGTERM handlers; here `asyncio.run` owns Ctrl+C and cancels the main
    task, whose `finally` asks the server to exit."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def call(method: str, url: str) -> dict:
    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


async def http(method: str, port: int, path: str) -> dict:
    return await asyncio.to_thread(call, method, f"http://127.0.0.1:{port}{path}")


# -------------------------------------------------------------------------------------------------- process census

def process_table() -> dict[int, tuple[int, str]]:
    """pid -> (parent pid, name) for every process (PowerShell on Windows, ps elsewhere)."""
    if os.name == "nt":
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name | ConvertTo-Json"],
            capture_output=True, text=True, timeout=60, check=True).stdout
        return {r["ProcessId"]: (r["ParentProcessId"], r["Name"]) for r in json.loads(out)}
    out = subprocess.run(["ps", "-eo", "pid=,ppid=,comm="], capture_output=True, text=True, check=True).stdout
    table = {}
    for line in out.splitlines():
        pid, ppid, name = line.split(None, 2)
        table[int(pid)] = (int(ppid), name)
    return table


def descendants(table: dict[int, tuple[int, str]], root: int) -> dict[int, str]:
    found: dict[int, str] = {}
    grew = True
    while grew:
        grew = False
        for pid, (ppid, name) in table.items():
            if pid not in found and (ppid == root or ppid in found):
                found[pid] = name
                grew = True
    return found


# ------------------------------------------------------------------------------------------------------- scenarios

async def human_local(page, base: str) -> None:
    await page.goto(f"{base}/societe/list.php")
    await page.get_by_role("textbox", name="Name filter").click()
    await page.keyboard.type(SENTINEL)
    await page.get_by_role("combobox", name="Limit").select_option("25")
    await page.keyboard.press("Enter")  # implicit submit: keydown Enter + submit + a full-page GET
    await page.wait_for_url("**/societe/list.php?*")
    await page.get_by_role("link", name="Aurora Seed Supplies").click()
    await page.get_by_role("textbox", name="Note").click()
    await page.keyboard.type(SENTINEL)
    await page.get_by_role("button", name="Save").click()  # full-page POST
    await page.wait_for_load_state()
    await page.get_by_role("link", name="Validate").click()  # a confirm_* GET
    await page.wait_for_url("**/compta/facture/card.php*")


async def human_dolibarr(page, base: str) -> None:
    from dolirest import Rest, customer_by_code  # noqa: F401  (target helpers: spike only, never production code)

    await page.goto(f"{base}/societe/list.php?type=c")
    await page.locator("input[name=search_customer_code]").click()
    await page.keyboard.type("CU2601-00901")
    await page.keyboard.press("Enter")  # keydown Enter + submit + full-page GET
    await page.get_by_role("link", name="Aurora Seed Supplies").click()
    await page.get_by_role("link", name="Customer", exact=True).click()
    await page.goto(f"{base}/compta/facture/list.php?search_ref_client=CUA-SEED-ALPHA-1")
    await page.locator("#id-right a[href*='/compta/facture/card.php?id=']").first.click()
    # "Clone" opens Dolibarr's confirmation dialog; Yes loads ?action=confirm_clone&confirm=yes (creates a draft;
    # the run resets the target afterwards)
    await page.get_by_role("link", name=re.compile(r"^clone$", re.I)).first.click()
    await page.get_by_role("button", name="Yes").click()
    await page.wait_for_load_state()


async def main(args: argparse.Namespace, results: list[tuple[str, bool, str]], census: dict) -> None:
    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, bool(ok), detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)

    fake = FakeTarget() if args.target == "local" else None
    run_dir = Path(tempfile.mkdtemp(prefix="s2-"))
    if fake:
        policy_path = run_dir / "policy.json"
        policy_path.write_text(json.dumps({
            "version": "cua.policy/v1", "name": "s2-local", "sandbox": True, "origins": ["http://localhost:${DOLIBARR_PORT}"],
            "routes": {"allow": ["/**"]}, "actions": ["navigate", "click", "type", "press", "select", "extract"],
            "max_effect": "read"}), encoding="utf-8")
        cfg = Settings(dolibarr_port=fake.port, cua_run_login="tester", cua_run_password=LOCAL_PASSWORD)
        kwargs = {"policy": str(policy_path), "config": cfg}
    else:
        kwargs = {}

    control = Control()
    port = free_port()
    server = NoSignalServer(uvicorn.Config(build_app(control), host="127.0.0.1", port=port, log_level="warning"))
    server_task = asyncio.create_task(server.serve())
    agent: asyncio.Task | None = None
    try:
        while not server.started:
            await asyncio.sleep(0.05)
        async with open_session(run_id="s2-spike", headed=args.headed, evidence_root=run_dir, **kwargs) as session:
            page = session.surface.page
            context = page.context
            base = session.base_url
            await context.expose_binding("__cuaCapture", control.on_capture)
            await context.add_init_script(CAPTURE_JS)
            context.on("request", control.on_request)

            # --- pause / resume -------------------------------------------------------------------------------
            urls = [f"{base}/societe/list.php?type=c", f"{base}/index.php?mainmenu=home&leftmenu=home"]

            async def automation() -> None:
                from cua.safety.policy import ActionRequest

                while True:
                    await control.running.wait()  # a step boundary: the only place a pause takes effect
                    url = urls[control.steps % 2]
                    request = ActionRequest(kind="navigate", actor="replay", page_url=url, target_url=url,
                                            effect="none", lease=session.lease())
                    await session.surface.navigate(url, request)
                    control.steps += 1

            agent = asyncio.create_task(automation())
            while control.steps < 3:
                await asyncio.sleep(0.1)
            await http("POST", port, "/pause")
            await asyncio.sleep(1.0)  # let the step in flight finish
            frozen = (await http("GET", port, "/status"))["steps"]
            await asyncio.sleep(2.0)
            still = (await http("GET", port, "/status"))["steps"]
            check("pause holds the loop at a step boundary", still == frozen and not (await http("GET", port, "/status"))["running"],
                  f"steps {frozen} -> {still} while paused")
            await http("POST", port, "/resume")
            await asyncio.sleep(2.0)
            moved = (await http("GET", port, "/status"))["steps"]
            check("resume continues the loop", moved > still, f"steps {still} -> {moved}")
            await http("POST", port, "/pause")  # hand over: automation is parked, a human takes the lease
            await asyncio.sleep(1.0)
            agent.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await agent

            # --- the human phase ------------------------------------------------------------------------------
            control.phase = "human"
            try:
                await (human_local if fake else human_dolibarr)(page, base)
                human_error = ""
            except Exception as e:  # noqa: BLE001 - the spike reports what the page did
                human_error = f"{type(e).__name__}: {str(e).splitlines()[0][:160]}"
            await asyncio.sleep(0.5)
            control.phase = "automation"
            kinds = [c["kind"] for c in control.captured]
            check("human scenario ran to the end", not human_error, human_error)
            for kind in ("click", "change", "enter", "submit"):
                check(f"captured {kind}", kind in kinds, f"{kinds.count(kind)}x")
            methods = {c.get("method") for c in control.captured if c["kind"] == "submit"}
            check("captured submits across full-page loads", len(control.captured) > 0 and bool(methods - {None}),
                  f"submit methods {sorted(m for m in methods if m)}; pages {sorted({c['path'] for c in control.captured})}")
            if fake:
                check("captured the full-page POST submit", "post" in methods, "")
            dump = json.dumps(control.captured)
            check("typed values never leave the page", SENTINEL not in dump and "SPIKE-TOKEN" not in dump,
                  f"{sum(1 for c in control.captured if c.get('value') == '[REDACTED]')} redacted values")
            check("confirm_* GET logged for the human", any(c["phase"] == "human" for c in control.confirms),
                  json.dumps(control.confirms))
            check("confirm token never logged", all(not c["token_logged"] for c in control.confirms), "")
            session.evidence.json("s2-capture.json", {"captured": control.captured, "confirms": control.confirms})

            census["before"] = descendants(process_table(), os.getpid())
            print(f"descendant processes while running: {len(census['before'])}", flush=True)

            # --- shutdown -------------------------------------------------------------------------------------
            print(f"shutdown by {args.interrupt}", flush=True)
            if args.interrupt == "sigint":
                asyncio.get_running_loop().call_later(0.2, signal.raise_signal, signal.SIGINT)
            else:
                asyncio.get_running_loop().call_later(0.2, asyncio.current_task().cancel)
            await asyncio.Event().wait()  # parked, as the real run is while a human works; only the signal ends this
    finally:
        if agent is not None:
            agent.cancel()
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(server_task, 10)
        if fake:
            fake.close()
        census["port"] = port
        if args.target == "dolibarr":
            await asyncio.to_thread(reset_target)


def reset_target() -> None:
    from dolictl import CtlError
    from livetarget import reset_to_baseline

    try:
        reset_to_baseline()
    except CtlError as e:
        if "15 s budget" not in str(e):  # the restore itself completed; only S10's time budget was exceeded
            raise
        print("restore finished over S10's 15 s budget (host load)", flush=True)
    print("target reset to baseline", flush=True)


def entry() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", choices=["dolibarr", "local"], default="dolibarr")
    parser.add_argument("--headless", dest="headed", action="store_false")
    parser.add_argument("--interrupt", choices=["sigint", "cancel"], default="sigint")
    args = parser.parse_args()
    if args.target == "dolibarr":
        print(f"target {settings().browser_base_url}")
    results: list[tuple[str, bool, str]] = []
    census: dict = {}
    try:
        asyncio.run(main(args, results, census))
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("interrupted: shutdown path ran")
    else:
        print("main returned without an interrupt")
        results.append(("interrupt ended the run", False, ""))

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, bool(ok), detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")

    if census.get("before") is not None:
        after = process_table()
        orphans = {pid: n for pid, n in census["before"].items() if pid in after and after[pid][1] == n}
        check("no orphan node/browser processes", not orphans, f"left: {orphans}" if orphans else
              f"{len(census['before'])} descendants all gone")
        with socket.socket() as s:
            s.settimeout(1)
            check("uvicorn port released", s.connect_ex(("127.0.0.1", census["port"])) != 0)
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(entry())
