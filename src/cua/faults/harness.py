"""Fault harness: network-level faults for the sandbox target, installed as Playwright routes.

Owner: Agent C. Sandbox-only: `install_faults` refuses (`PolicyDenied`, rule `sandbox_only`) unless the policy marks
the target as a sandbox. Imports: playwright, pydantic and `cua.safety.policy` (for `Policy` / `PolicyDenied`) only.
`replay`, `surface` and `safety` never import this package; only tests and the CLI install faults.

Faults are routes on the guarded context, registered AFTER the guard's own route (`cua.safety.network`), so they run
first (later-registered routes run first) and must cooperate with it:

- a fault handler handles only the requests its spec matches; everything else goes to `route.fallback()`, so the
  guard sees it unchanged;
- a matching request whose URL the policy refuses is NOT faulted: it also goes to `route.fallback()`, and the guard
  blocks and reports it. A fault pattern can therefore never widen what the browser may reach;
- before any `route.fetch()` the handler has vetted `policy.check_url(url)`, and it never calls `route.continue_()`;
- `slow` ends in `fallback()`, so it composes with the other faults and the guard; the others are terminal. Faults
  see a request in list order, so a terminal fault shadows the ones listed after it.

Fault kinds (closed set, `extra="forbid"`): `slow` (delay), `app_error` (fulfil a 500 page), `interstitial` (real HTML
plus a modal a profile knows), `unknown_dialog` (real HTML plus a modal no profile knows) and
`lost_response_after_commit` (the real request is sent, the browser is told 504). Each spec matches by `match` (a path
glob such as `/compta/paiement.php`, or a URL glob such as `http://127.0.0.1:8087/societe/*`; `**` crosses `/`, `*`
does not), optional `methods`, optional `body_fields` (url-encoded form fields that must all equal), and an optional
`times` limit. Every firing is reported through `on_fault` as a `FaultEvent` (declared: these are injected, not found),
which the caller turns into an evidence event `kind="fault"`.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import re
from collections.abc import Callable
from functools import lru_cache
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl, urlsplit

from playwright.async_api import BrowserContext, Request, Route
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from cua.safety.policy import Policy

_FETCH_TIMEOUT_MS = 60_000
_HOP_HEADERS = frozenset({"content-length", "content-encoding", "transfer-encoding"})
_BODY_END = re.compile(rb"</body\s*>", re.IGNORECASE)

FaultKind = Literal["slow", "app_error", "interstitial", "unknown_dialog", "lost_response_after_commit"]


# ------------------------------------------------------------------------------------------------------- the specs


class _FaultBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    match: str = Field(min_length=1)  # path glob ("/compta/**") or absolute URL glob ("http://host:port/x/*")
    methods: list[str] | None = None  # None = any method; compared upper-case
    body_fields: dict[str, str] = Field(default_factory=dict)  # url-encoded form fields that must all be present
    times: int | None = Field(default=None, ge=1)  # fire at most this many times; None = every match


class SlowFault(_FaultBase):
    kind: Literal["slow"] = "slow"
    ms: int = Field(ge=0, le=60_000)


class AppErrorFault(_FaultBase):
    kind: Literal["app_error"] = "app_error"
    status: int = Field(default=500, ge=500, le=599)


class InterstitialFault(_FaultBase):
    """A modal the profile knows: stable id, a dialog role, a title and a dismiss button."""

    kind: Literal["interstitial"] = "interstitial"
    title: str = "Service notice"
    message: str = "Scheduled maintenance tonight."
    button: str = "Dismiss"


class UnknownDialogFault(_FaultBase):
    """A modal no profile knows: a different shape (two buttons, an unfamiliar title) and its own id."""

    kind: Literal["unknown_dialog"] = "unknown_dialog"
    title: str = "Quick survey"
    message: str = "How was your experience today?"
    buttons: list[str] = Field(default_factory=lambda: ["Maybe later", "Take the survey"], min_length=1)


class LostResponseFault(_FaultBase):
    """The commit reaches the server, the browser never learns: `route.fetch()` then `route.fulfill(504)`."""

    kind: Literal["lost_response_after_commit"] = "lost_response_after_commit"
    methods: list[str] | None = Field(default_factory=lambda: ["POST"])
    status: int = Field(default=504, ge=500, le=599)


FaultSpec = Annotated[
    SlowFault | AppErrorFault | InterstitialFault | UnknownDialogFault | LostResponseFault, Field(discriminator="kind")
]
_SPEC = TypeAdapter(FaultSpec)


def parse_fault(kind: str, *, match: str, **fields: Any) -> FaultSpec:
    """Build a spec from the plan's notation: `parse_fault("slow:250", match="/societe/**")`."""
    name, _, arg = kind.partition(":")
    if name == "slow":
        if not arg.isdigit():
            raise ValueError("slow needs a delay in milliseconds, e.g. 'slow:250'")
        fields["ms"] = int(arg)
    elif arg:
        raise ValueError(f"fault kind {name!r} takes no argument")
    return _SPEC.validate_python({"kind": name, "match": match, **fields})


# ------------------------------------------------------------------------------------------------------- the events


class FaultEvent(BaseModel):
    """One firing. `url` carries no query, fragment or userinfo. `declared` is always true: the fault was injected."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: FaultKind
    index: int  # position of the spec in the installed list
    fired: int  # how many times this spec has fired, this one included
    url: str
    method: str
    detail: dict[str, Any] = Field(default_factory=dict)
    declared: Literal[True] = True


FaultHandler = Callable[[FaultEvent], None]


class FaultInstallation:
    """The routes `install_faults` registered, with what fired. `uninstall()` removes them."""

    def __init__(self, context: BrowserContext, handlers: list[Callable[..., Any]]) -> None:
        self._context = context
        self._handlers = handlers
        self.fired: list[FaultEvent] = []

    def count(self, index: int) -> int:
        return sum(1 for e in self.fired if e.index == index)

    async def uninstall(self) -> None:
        handlers, self._handlers = self._handlers, []
        for handler in handlers:
            with contextlib.suppress(Exception):
                await self._context.unroute("**/*", handler)


# ------------------------------------------------------------------------------------------------------ the matching


@lru_cache(maxsize=256)
def _glob(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out), re.DOTALL)


def _loggable(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        try:
            port = f":{parts.port}" if parts.port else ""
        except ValueError:
            port = ":invalid-port"
        return f"{parts.scheme.lower()}://{host}{port}{parts.path}"[:200]
    except Exception:  # noqa: BLE001 - total by contract
        return "invalid-url"


def _post_fields(request: Request) -> dict[str, str]:
    try:
        data = request.post_data
    except Exception:  # noqa: BLE001 - a binary body is not a form
        return {}
    return dict(parse_qsl(data or "", keep_blank_values=True))


def _matches(spec: _FaultBase, request: Request) -> bool:
    if spec.methods is not None and request.method.upper() not in {m.upper() for m in spec.methods}:
        return False
    parts = urlsplit(request.url)
    target = parts.path if spec.match.startswith("/") else f"{parts.scheme}://{parts.netloc}{parts.path}"
    if not _glob(spec.match).fullmatch(target):
        return False
    if spec.body_fields:
        fields = _post_fields(request)
        return all(fields.get(k) == v for k, v in spec.body_fields.items())
    return True


def _is_top_document(request: Request) -> bool:
    try:
        return request.resource_type == "document" and request.frame.parent_frame is None
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------------------------------- the markup


def _modal(dialog_id: str, title: str, message: str, buttons: list[str]) -> bytes:
    heading = f"{dialog_id}-title"
    buttons_html = "".join(
        f'<button type="button" onclick="this.closest(\'[role=dialog]\').parentNode.remove()">{html.escape(b)}</button>'
        for b in buttons
    )
    return (
        f'<div style="position:fixed;inset:0;z-index:2147483647;background:rgba(0,0,0,.5);display:flex;'
        f'align-items:center;justify-content:center"><div id="{dialog_id}" role="dialog" aria-modal="true" '
        f'aria-labelledby="{heading}" style="background:#fff;color:#000;padding:24px;border-radius:6px">'
        f'<h2 id="{heading}">{html.escape(title)}</h2><p>{html.escape(message)}</p>{buttons_html}</div></div>'
    ).encode("utf-8")


def _markup(spec: _FaultBase) -> bytes:
    if isinstance(spec, InterstitialFault):
        return _modal("cua-fault-interstitial", spec.title, spec.message, [spec.button])
    assert isinstance(spec, UnknownDialogFault)
    return _modal("cua-fault-unknown-dialog", spec.title, spec.message, spec.buttons)


def _inject(body: bytes, markup: bytes) -> bytes | None:
    ends = list(_BODY_END.finditer(body))
    if not ends:
        return None
    at = ends[-1].start()
    return body[:at] + markup + body[at:]


def _error_page(status: int) -> bytes:
    return (
        f"<!DOCTYPE html><html><head><title>Error {status}</title></head><body><h1>Internal Server Error</h1>"
        f"<p>Injected fault: the application failed ({status}).</p></body></html>"
    ).encode()


# ----------------------------------------------------------------------------------------------------- installation


async def install_faults(
    context: BrowserContext, policy: Policy, faults: list[FaultSpec], *, on_fault: FaultHandler
) -> FaultInstallation:
    """Register `faults` as routes on `context` (the guarded context from `new_guarded_context`).

    Raises `PolicyDenied` (rule `sandbox_only`) before touching the context unless `policy.sandbox` is true.
    `on_fault` is called synchronously for every firing and never raises into the browser.
    """
    policy.require_sandbox()
    installation = FaultInstallation(context, [])

    # Later-registered routes run first, so register in reverse: the list order is the order faults get the request
    # (a `slow` listed before a terminal fault delays it; a terminal fault shadows the faults listed after it).
    for index, spec in reversed(list(enumerate(faults))):
        handler = _make_handler(index, spec, policy, installation, on_fault)
        await context.route("**/*", handler)  # after the guard's route, so it runs first
        installation._handlers.append(handler)
    return installation


def _make_handler(
    index: int, spec: _FaultBase, policy: Policy, installation: FaultInstallation, on_fault: FaultHandler
) -> Callable[[Route, Request], Any]:
    reserved = 0  # firings claimed (a claim is refunded when the fault turns out not to apply)

    def claim() -> bool:
        nonlocal reserved
        if spec.times is not None and reserved >= spec.times:
            return False
        reserved += 1
        return True

    def refund() -> None:
        nonlocal reserved
        reserved -= 1

    def report(request: Request, **detail: Any) -> None:
        event = FaultEvent(
            kind=spec.kind,  # type: ignore[attr-defined]
            index=index,
            fired=installation.count(index) + 1,
            url=_loggable(request.url),
            method=request.method,
            detail=detail,
        )
        installation.fired.append(event)
        with contextlib.suppress(Exception):  # evidence must never decide what the browser sees
            on_fault(event)

    async def handler(route: Route, request: Request) -> None:
        try:
            applies = _matches(spec, request) and policy.check_url(request.url).allowed
        except Exception:  # noqa: BLE001
            applies = False
        if not applies or not claim():
            await route.fallback()  # not ours, or not allowed: the guard (or the next fault) decides
            return
        try:
            await _apply(spec, route, request, report, refund)
        except Exception:  # noqa: BLE001 - a fault bug must not leave the request hanging
            with contextlib.suppress(Exception):
                await route.abort("failed")

    return handler


async def _apply(
    spec: _FaultBase,
    route: Route,
    request: Request,
    report: Callable[..., None],
    refund: Callable[[], None],
) -> None:
    if isinstance(spec, SlowFault):
        report(request, ms=spec.ms)
        await asyncio.sleep(spec.ms / 1000)
        await route.fallback()
    elif isinstance(spec, AppErrorFault):
        report(request, status=spec.status)
        await route.fulfill(
            status=spec.status, headers={"content-type": "text/html; charset=utf-8"}, body=_error_page(spec.status)
        )
    elif isinstance(spec, LostResponseFault):
        # the url was vetted by the caller; the real request is sent without following redirects
        response = await route.fetch(max_redirects=0, timeout=_FETCH_TIMEOUT_MS)
        report(request, status=spec.status, server_status=response.status)
        await route.fulfill(status=spec.status, headers={"content-type": "text/plain"}, body="Gateway Time-out")
    else:
        assert isinstance(spec, InterstitialFault | UnknownDialogFault)
        if not _is_top_document(request):
            refund()
            await route.fallback()
            return
        response = await route.fetch(max_redirects=0, timeout=_FETCH_TIMEOUT_MS)
        body = await response.body()
        content_type = response.headers.get("content-type", "")
        injected = _inject(body, _markup(spec)) if response.status < 300 and "text/html" in content_type else None
        if injected is None:  # not an HTML page we can modify: hand over the real response, unchanged
            refund()
            await route.fulfill(response=response)
            return
        headers = {k: v for k, v in response.headers.items() if k.lower() not in _HOP_HEADERS}
        report(request, server_status=response.status)
        await route.fulfill(status=response.status, headers=headers, body=injected)
