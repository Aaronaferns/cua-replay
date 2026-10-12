"""Fixtures for the fault tests: local HTTP servers that record every hit (method, path, body) and a headless Chromium."""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest
from playwright.async_api import Browser, async_playwright


@dataclass
class HitServer:
    """A local server on an ephemeral port. `hits` is the ground truth for 'did this request reach the server'."""

    pages: dict[str, tuple[int, str, str]] = field(default_factory=dict)
    hits: list[str] = field(default_factory=list)
    bodies: list[str] = field(default_factory=list)
    httpd: ThreadingHTTPServer | None = None

    @property
    def origin(self) -> str:
        assert self.httpd is not None
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def serve(self, path: str, body: str, *, status: int = 200, ctype: str = "text/html; charset=utf-8") -> None:
        self.pages[path] = (status, body, ctype)

    def count(self, prefix: str) -> int:
        return sum(1 for h in self.hits if h.split(" ", 1)[1].startswith(prefix))


def _start(server: HitServer) -> None:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _handle(self):
            length = int(self.headers.get("Content-Length") or 0)
            server.bodies.append(self.rfile.read(length).decode() if length else "")
            server.hits.append(f"{self.command} {self.path}")
            path = urlsplit(self.path).path
            status, body, ctype = server.pages.get(
                path, (200, f"<html><body><h1>{path}</h1></body></html>", "text/html; charset=utf-8")
            )
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = _handle

    server.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.httpd.serve_forever, daemon=True).start()


@pytest.fixture
def servers() -> Iterator[Callable[[], HitServer]]:
    started: list[HitServer] = []

    def make() -> HitServer:
        server = HitServer()
        _start(server)
        started.append(server)
        return server

    yield make
    for server in started:
        assert server.httpd is not None
        server.httpd.shutdown()
        server.httpd.server_close()


@pytest.fixture
async def browser() -> AsyncIterator[Browser]:
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        yield b
        await b.close()
