"""Fixtures for the control tests: a local site that records what it receives, and a headless Chromium."""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest
from playwright.async_api import Browser, async_playwright

LOGIN = """<!DOCTYPE html><html><head><title>Login</title></head><body>
<form method="POST" action="/login.php">
<input type="text" name="username" aria-label="Login">
<input type="password" name="password" aria-label="Password">
<input type="text" name="code" aria-label="Security code">
<select name="lang" aria-label="Language"><option value="en">English</option><option value="de">Deutsch</option></select>
<input type="submit" value="Login"></form></body></html>"""

HOME = """<!DOCTYPE html><html><head><title>Home</title></head><body>
<a href="/list.php?q=alpha&amp;token=SECRETTOKEN1234">Customers</a>
<form method="POST" action="/pay.php?token=SECRETTOKEN1234">
<input type="hidden" name="action" value="confirm_paiement"><input type="text" name="amount" aria-label="Amount">
<input type="submit" value="Pay"></form>
<form method="POST" action="/note.php"><input type="hidden" name="action" value="save_note">
<input type="text" name="note" aria-label="Note"><input type="submit" value="Save note"></form>
</body></html>"""


@dataclass
class Site:
    hits: list[str] = field(default_factory=list)
    bodies: list[str] = field(default_factory=list)
    httpd: ThreadingHTTPServer | None = None

    @property
    def origin(self) -> str:
        assert self.httpd is not None
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"


@pytest.fixture
def site() -> Iterator[Site]:
    state = Site()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, html: str) -> None:
            data = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            state.hits.append(f"GET {self.path}")
            self._send(LOGIN if urlsplit(self.path).path == "/login.php" else HOME)

        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            state.hits.append(f"POST {self.path}")
            state.bodies.append(body)
            self._send(HOME)

    state.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=state.httpd.serve_forever, daemon=True).start()
    yield state
    state.httpd.shutdown()
    state.httpd.server_close()


@pytest.fixture
async def browser() -> AsyncIterator[Browser]:
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        yield b
        await b.close()
