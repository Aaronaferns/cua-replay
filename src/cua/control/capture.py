"""Capturing what a human does on the live session, and logging the requests they cause. Ported from spike S2.

Owner: Agent C. While a human holds the lease the browser is theirs; this module records, for the evidence bundle and for
the resume decision, WHAT they did, never WHAT THEY TYPED.

`install_capture(context, *, on_event)` exposes a binding (`__cuaCapture`) and an init script on the browser context, and
runs the script on the pages already open (the human often takes over a page that is already showing, e.g. the login
CAPTCHA). The script reports `click`, `change`, `submit` and `enter` (Enter in an input or select) with a target
descriptor (tag, type, role, name or id, label, and a button's or link's text) across full-page navigations and POSTs.
Values are replaced by `[REDACTED]` inside the page, so a typed value (the CAPTCHA included) never reaches Python; the
Python side then enforces the same rules again, because the page is the application's, not ours: only the four event
kinds are accepted, unknown fields are dropped, every string is clipped, and `value` is forced to `[REDACTED]` on
`change` and `submit` whatever the page sent. A page that forges a payload therefore cannot smuggle a value out through
this channel. Query strings never appear (`path` is the pathname). Every event is tagged `actor: "human"`.

`RequestLog` records the requests the human's actions cause (documents, XHR, fetch) as `RequestLogEntry`s with a
query-free URL, and marks each one that matches a profile commit route via `cua.artifact.routes.is_commit`: a request
whose body cannot be read as a url-encoded form counts as unknown fields, which `is_commit` treats as possibly committing.
The resume decision (M5, not here) reads `RequestLog.commit_hits` and the captured clicks.

Both take an optional `active` predicate (typically "the lease holder is a human") and drop events while it is false, since
a binding and an init script cannot be taken back off a context; `close()` stops forwarding for good. Install at most once
per context (Playwright refuses to expose the same binding twice).
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import parse_qsl

from playwright.async_api import BrowserContext, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from cua.artifact.routes import is_commit
from cua.artifact.schema import Profile
from cua.control.lease import LeaseToken
from cua.evidence.events import EvidenceEvent, EvidenceWriter
from cua.safety.network import loggable_url

REDACTED = "[REDACTED]"
BINDING = "__cuaCapture"
_MAX_TEXT = 80
_MAX_FIELDS = 60

CAPTURE_JS = r"""
(() => {
  if (window.__cuaCaptureInstalled) return;
  window.__cuaCaptureInstalled = true;
  const clip = (s, n) => (s == null ? null : String(s).slice(0, n));
  const label = el => clip((el.labels && el.labels[0] && el.labels[0].innerText) || el.getAttribute('aria-label')
      || el.getAttribute('placeholder') || '', 40);
  const isButtonInput = el => el.tagName === 'INPUT' && /^(submit|button|reset|image)$/i.test(el.type || '');
  const base = (kind, el) => ({
    kind, tag: (el.tagName || '').toLowerCase(), type: el.type || null, role: el.getAttribute('role') || null,
    name: el.name || el.id || null, label: label(el), path: location.pathname, frame: window !== window.top,
    t: Date.now(),
  });
  const send = o => { try { window[BINDING_NAME](JSON.stringify(o)); } catch (e) {} };
  document.addEventListener('click', e => {
    const el = e.target.closest('a,button,input,select,textarea,[role=button]') || e.target;
    const o = base('click', el);
    // a button or link's own text is its label; a field's value is never read
    if (el.tagName === 'A' || el.tagName === 'BUTTON') o.text = clip((el.innerText || '').trim(), 40);
    else if (isButtonInput(el)) o.text = clip(el.value, 40);
    send(o);
  }, true);
  document.addEventListener('change', e => {
    const o = base('change', e.target);
    o.value = '[REDACTED]';
    send(o);
  }, true);
  document.addEventListener('keydown', e => {
    if (e.key === 'Enter' && /^(INPUT|SELECT)$/.test(e.target.tagName)) send(base('enter', e.target));
  }, true);
  document.addEventListener('submit', e => {
    const f = e.target, o = base('submit', f);
    // getAttribute, not f.action / f.method: a form's field named "action" (every Dolibarr form has one) shadows them
    o.method = (f.getAttribute('method') || 'get').toLowerCase();
    o.action = new URL(f.getAttribute('action') || '', location.href).pathname;
    o.fields = Array.from(f.elements).filter(x => x.name).map(x => x.name);
    o.value = '[REDACTED]';
    send(o);
  }, true);
})();
""".replace("BINDING_NAME", f"'{BINDING}'")

EventKind = Literal["click", "change", "submit", "enter"]


class CaptureEvent(BaseModel):
    """One human action, as the page reported it and Python re-validated it. Never carries a typed value."""

    model_config = ConfigDict(extra="ignore", frozen=True, hide_input_in_errors=True)

    actor: Literal["human"] = "human"
    kind: EventKind
    tag: str | None = None
    type: str | None = None
    role: str | None = None
    name: str | None = None  # the control's name attribute or id
    label: str | None = None
    text: str | None = None  # a link or button's own label
    path: str | None = None  # location.pathname: no query
    frame: bool = False
    method: str | None = None  # submit
    action: str | None = None  # submit: the form's target pathname
    fields: list[str] = Field(default_factory=list)  # submit: field names only
    value: Literal["[REDACTED]"] | None = None  # change and submit: always this constant
    t: int | None = None  # the page's clock, ms
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("tag", "type", "role", "name", "label", "text", "path", "method", "action", mode="before")
    @classmethod
    def _clip(cls, v: Any) -> Any:
        return v[:_MAX_TEXT] if isinstance(v, str) else None

    @field_validator("fields", mode="before")
    @classmethod
    def _fields(cls, v: Any) -> Any:
        return [x[:_MAX_TEXT] for x in v[:_MAX_FIELDS] if isinstance(x, str)] if isinstance(v, list) else []

    @field_validator("t", mode="before")
    @classmethod
    def _t(cls, v: Any) -> Any:
        return v if isinstance(v, int) and not isinstance(v, bool) else None

    def descriptor(self) -> dict[str, Any]:
        """The target and action as a plain dict for an evidence event's `action` field."""
        data = self.model_dump(mode="json", exclude={"ts", "t"}, exclude_none=True)
        data["fields"] = data.get("fields") or []
        return data


def parse_payload(payload: object) -> CaptureEvent | None:
    """Re-validate what the page sent. Anything that is not one of the four event kinds is dropped."""
    try:
        data = json.loads(payload) if isinstance(payload, str) else None
        if not isinstance(data, dict) or data.get("kind") not in ("click", "change", "submit", "enter"):
            return None
        data.pop("actor", None)
        data.pop("ts", None)
        data["value"] = REDACTED if data["kind"] in ("change", "submit") else None
        return CaptureEvent.model_validate(data)
    except (ValueError, TypeError):
        return None


class CaptureInstallation:
    """Handle for an installed capture. `close()` stops forwarding (the binding itself stays on the context)."""

    def __init__(self, on_event: Callable[[CaptureEvent], None], active: Callable[[], bool] | None) -> None:
        self._on_event = on_event
        self._active = active
        self._closed = False
        self.dropped = 0  # events seen while inactive or closed

    def close(self) -> None:
        self._closed = True

    async def _binding(self, source: Any, payload: str) -> None:
        event = parse_payload(payload)
        if event is None:
            return
        if self._closed or (self._active is not None and not self._active()):
            self.dropped += 1
            return
        with contextlib.suppress(Exception):  # evidence must never break the human's page
            self._on_event(event)


async def install_capture(
    context: BrowserContext,
    *,
    on_event: Callable[[CaptureEvent], None],
    active: Callable[[], bool] | None = None,
) -> CaptureInstallation:
    """Install the capture binding and init script on `context` and on every page and frame already open.

    `on_event` is called synchronously with each `CaptureEvent` (actor `human`); its failures are swallowed.
    """
    installation = CaptureInstallation(on_event, active)
    await context.expose_binding(BINDING, installation._binding)
    await context.add_init_script(CAPTURE_JS)
    for page in context.pages:
        for frame in page.frames:
            with contextlib.suppress(Exception):  # a frame that is navigating or gone is covered by the init script
                await frame.evaluate(CAPTURE_JS)
    return installation


def evidence_sink(writer: EvidenceWriter, lease: Callable[[], LeaseToken]) -> Callable[[CaptureEvent], None]:
    """An `on_event` that writes each captured action as an `action` evidence event, actor `human`, under the
    current lease token."""

    def sink(event: CaptureEvent) -> None:
        writer.event(
            EvidenceEvent(
                ts=event.ts, seq=0, run_id="", actor="human", kind="action", lease=lease(),
                action=event.descriptor(), detail={"source": "capture"},
            )
        )

    return sink


# ----------------------------------------------------------------------------------------------------- request log


class RequestLogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    actor: Literal["human"] = "human"
    method: str
    url: str  # no query, fragment or userinfo
    resource_type: str
    commit: bool  # matches a profile commit route (unreadable bodies count as possibly committing)
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))


_LOGGED_TYPES = frozenset({"document", "xhr", "fetch"})


def _form_fields(request: Request) -> dict[str, str] | None:
    """Url-encoded form fields of a request body, `{}` for a request without a body, `None` when they cannot be known."""
    if request.method.upper() in ("GET", "HEAD"):
        return {}
    try:
        body = request.post_data
        content_type = (request.headers.get("content-type") or "").lower()
    except Exception:  # noqa: BLE001 - binary or unavailable body
        return None
    if body is None or body == "":
        return {}
    if "application/x-www-form-urlencoded" in content_type:
        return dict(parse_qsl(body, keep_blank_values=True))
    return None  # multipart, JSON, ...: not readable as fields


class RequestLog:
    """Records the requests made while a human holds the lease and flags the ones that commit.

    `entries` is every logged request; `commit_hits` the flagged ones. `on_commit` is called synchronously for each hit.
    Call `attach(context)` when the human takes over and `detach()` when control leaves them (or pass `active`).
    """

    def __init__(
        self,
        profile: Profile,
        *,
        on_commit: Callable[[RequestLogEntry], None] | None = None,
        active: Callable[[], bool] | None = None,
    ) -> None:
        self._profile = profile
        self._on_commit = on_commit
        self._active = active
        self._context: BrowserContext | None = None
        self.entries: list[RequestLogEntry] = []

    @property
    def commit_hits(self) -> list[RequestLogEntry]:
        return [e for e in self.entries if e.commit]

    def attach(self, context: BrowserContext) -> None:
        if self._context is None:
            self._context = context
            context.on("request", self._on_request)

    def detach(self) -> None:
        if self._context is not None:
            with contextlib.suppress(Exception):
                self._context.remove_listener("request", self._on_request)
            self._context = None

    def _on_request(self, request: Request) -> None:
        try:
            if request.resource_type not in _LOGGED_TYPES or (self._active is not None and not self._active()):
                return
            entry = RequestLogEntry(
                method=request.method.upper(),
                url=loggable_url(request.url),
                resource_type=request.resource_type,
                commit=is_commit(self._profile, request.method, request.url, _form_fields(request)),
            )
        except Exception:  # noqa: BLE001 - logging must never break the page
            return
        self.entries.append(entry)
        if entry.commit and self._on_commit is not None:
            with contextlib.suppress(Exception):
                self._on_commit(entry)


def commit_hit_sink(writer: EvidenceWriter, lease: Callable[[], LeaseToken]) -> Callable[[RequestLogEntry], None]:
    """An `on_commit` that writes a `commit_attempt` evidence event, actor `human`, for a flagged request."""

    def sink(entry: RequestLogEntry) -> None:
        writer.event(
            EvidenceEvent(
                ts=entry.ts, seq=0, run_id="", actor="human", kind="commit_attempt", lease=lease(),
                detail={"source": "request_log", "method": entry.method, "url": entry.url},
            )
        )

    return sink
