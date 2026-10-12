"""An in-memory fake surface: a tiny Dolibarr with a login wall, a customer list, a card, and failure knobs.

It implements the `Surface` protocol faithfully where replay depends on it: `find` returns every match (the caller
enforces exactly-one), actions pass the request to the guard first and raise `PolicyDenied` with the UI untouched,
and `observe` produces an outline snapshot from the page's elements.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

from cua.artifact.names import names_match
from cua.artifact.schema import Fingerprint, Strategy
from cua.control.lease import LeaseToken
from cua.safety.policy import ActionRequest, PolicyDecision, PolicyDenied
from cua.surface.base import Observation, PageFacts

BASE = "http://127.0.0.1:8086"
LEASE = LeaseToken(holder="automation", generation=1)
CUSTOMERS = {
    "CU1212-0005": {"name": "NLTechno", "amount": "75.37 €"},
    "CU1910-00021": {"name": "Company Corp 2", "amount": "1,234.50 €"},
}


def lease() -> LeaseToken:
    return LEASE


class Clock:
    """Virtual time: sleeping advances it, so settle timeouts cost nothing."""

    def __init__(self) -> None:
        self.ms = 0

    def now(self) -> int:
        return self.ms

    async def sleep(self, seconds: float) -> None:
        self.ms += int(seconds * 1000)


class AllowGuard:
    def __init__(self) -> None:
        self.requests: list[ActionRequest] = []

    def check(self, request: ActionRequest) -> PolicyDecision:
        self.requests.append(request)
        return PolicyDecision(allowed=True, rule="test_allow", reason="ok")


class DenyGuard(AllowGuard):
    def __init__(self, kinds: set[str], *, approvable: bool = False) -> None:
        super().__init__()
        self.kinds, self.approvable = kinds, approvable

    def check(self, request: ActionRequest) -> PolicyDecision:
        self.requests.append(request)
        if request.kind in self.kinds:
            return PolicyDecision(allowed=False, rule="action_type", reason=f"{request.kind} is not allowed",
                                  approvable=self.approvable)
        return PolicyDecision(allowed=True, rule="test_allow", reason="ok")


@dataclass
class El:
    role: str
    name: str | None = None
    text: str | None = None
    tag: str = "div"
    input_type: str | None = None
    row_label: str | None = None
    take: str | None = None
    column_header: str | None = None
    row_key: str | None = None
    css: str | None = None
    on_click: Callable[[], None] | None = None
    uid: int = 0
    ident: int = field(default=0)


@dataclass
class Page:
    url: str
    title: str
    elements: list[El]
    dialog: str | None = None
    native_dialog: str | None = None
    dialog_elements: list[El] = field(default_factory=list)


class FakeDolibarr:
    """Implements `cua.surface.base.Surface` over in-memory pages."""

    def __init__(self, guard: AllowGuard, *, logged_in: bool = True, password: str = "pw") -> None:
        self.guard = guard
        self.logged_in = logged_in
        self.password = password
        self.url = BASE + "/"
        self.epoch = 0
        self.loading = 0
        self.typed: dict[str, str] = {}
        self.log: list[str] = []
        # failure knobs
        self.row_key_visible = True
        self.link_tag = "a"
        self.duplicate_rows = False
        self.card_code_override: str | None = None
        self.amount_has_row_label = True
        self.expire_on_card = False
        self.expire_cards = 0  # expire the session on this many card loads, then stop
        self.native_on_card = False
        self.list_header = True
        self.card_has_value_cell = True
        self.delay_polls = 0
        self.amount_text_override: str | None = None
        self.captcha = False
        self.app_error_on_card = False
        self.notice: str | None = None  # a modal over the customer list: its title
        self.notice_buttons: list[str] = ["Dismiss"]
        self.access_denied_on_list = False
        self.hang_on_click = False
        self._next_id = 0

    # --- pages -------------------------------------------------------------------------------------------------

    def _el(self, **kw) -> El:
        self._next_id += 1
        return El(uid=self._next_id, **kw)

    def _goto(self, url: str) -> None:
        self.url = url
        self.loading = self.delay_polls

    def _login_page(self) -> Page:
        def submit() -> None:
            if self.typed.get("Login") == "admin" and self.typed.get("Password") == self.password:
                self.logged_in = True
                self._goto(self.url)

        extra = [self._el(role="textbox", name="Security code", tag="input", input_type="text")] if self.captcha else []
        return Page(self.url, "Login", extra + [
            self._el(role="textbox", name="Login", tag="input", input_type="text"),
            self._el(role="textbox", name="Password", tag="input", input_type="password"),
            self._el(role="button", name="Login", tag="input", input_type="submit", on_click=submit),
        ])

    def _render(self) -> Page:
        parts = urlsplit(self.url)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        if not self.logged_in:
            return self._login_page()
        if parts.path == "/societe/card.php" and (self.expire_on_card or self.expire_cards):
            self.expire_cards = max(self.expire_cards - 1, 0)
            self.logged_in = False
            return self._login_page()
        if parts.path in ("/index.php", "/"):
            return Page(self.url, "Home", [self._el(role="generic", text="Global view")])
        if parts.path == "/societe/list.php" and self.access_denied_on_list:
            return Page(self.url, "Access", [self._el(role="generic", text="Access denied.")])
        if parts.path == "/societe/card.php" and self.app_error_on_card:
            return Page(self.url, "Error 500", [self._el(role="heading", name="Internal Server Error")])
        if parts.path == "/societe/list.php":
            return self._list(query)
        if parts.path == "/societe/card.php":
            return self._card(query["code"])
        return Page(self.url, "Not found", [self._el(role="generic", text="Something else")])

    def _list(self, query: dict[str, str]) -> Page:
        code = query.get("search_code", "")
        elements = [self._el(role="textbox", name="Filter", tag="input", input_type="text",
                             column_header="Customer Code", css="input[name='search_code']")]
        if self.list_header:
            elements.append(self._el(role="columnheader", name="Third-party name"))
        rows = [c for c in CUSTOMERS if not code or c == code]
        if not rows:
            elements.append(self._el(role="cell", name="No record found"))
        for c in rows:
            for _ in range(2 if self.duplicate_rows else 1):
                elements.append(self._el(
                    role="link", name=CUSTOMERS[c]["name"], tag=self.link_tag, column_header="Third-party name",
                    row_key=c if self.row_key_visible else None, row_label="Customer Code", take="control",
                    on_click=lambda c=c: self._goto(f"{BASE}/societe/card.php?code={c}")))
        return Page(self.url, "Customers", elements)

    def _card(self, code: str) -> Page:
        if self.native_on_card:
            return Page(self.url, "Card", [], native_dialog="Are you sure?")
        shown_code = self.card_code_override or code
        elements = [self._el(role="cell", name="Nature of Third party"), self._el(role="cell", name="Customer Code")]
        if self.card_has_value_cell:
            elements.append(self._el(role="cell", text=f"{shown_code}  ", tag="td",
                                     row_label="Customer Code", take="value"))
        elements.append(self._el(
            role="link", name="Current outstanding bill", tag="a",
            text=self.amount_text_override or CUSTOMERS[code]["amount"],
            row_label="Current outstanding bill" if self.amount_has_row_label else None, take="value"))
        return Page(self.url, "Third party", elements)

    # --- Surface -----------------------------------------------------------------------------------------------

    def _page(self) -> Page:
        if self.loading > 0:
            return Page(self.url, "Loading", [self._el(role="generic", text="Loading...")])
        page = self._render()
        if self.notice and page.title == "Customers":
            def dismiss() -> None:
                self.notice = None

            page.dialog = self.notice
            page.dialog_elements = [self._el(role="button", name=b, tag="button", on_click=dismiss)
                                    for b in self.notice_buttons]
        return page

    async def observe(self, *, screenshot: bool = True) -> Observation:
        page = self._page()
        if self.loading > 0:
            self.loading -= 1
        self.epoch += 1
        lines = []
        for e in page.elements:
            head = f"- {e.role}" + (f' "{e.name}"' if e.name else "") + f" [ref=e{e.uid}]"
            lines.append(head + (f": {e.text}" if e.text else ""))
        dialog_lines = [f'- button "{e.name}" [ref=e{e.uid}]' for e in page.dialog_elements]
        return Observation(epoch=self.epoch, facts=PageFacts(url=page.url, title=page.title, dialog=page.dialog,
                                                             native_dialog=page.native_dialog),
                           snapshot="\n".join(lines) + "\n",
                           dialog_snapshot="\n".join(dialog_lines) + "\n" if page.dialog else None)

    async def settle(self, *, interval_ms: int = 150, timeout_ms: int = 5000) -> bool:
        """The navigation fence: records the call; true when nothing is loading (it never consumes `loading`)."""
        self.log.append("settle")
        return self.loading == 0

    async def element_for_ref(self, ref: str, epoch: int):  # pragma: no cover - discovery only
        raise NotImplementedError

    async def find(self, strategy: Strategy) -> list[El]:
        self.log.append(f"find {strategy.kind}")
        out = []
        page = self._page()
        for e in page.dialog_elements if page.dialog else page.elements:
            if strategy.kind == "role":
                ok = e.role == strategy.role and names_match(strategy.name or "", e.name)
            elif strategy.kind == "same_row":
                ok = (names_match(strategy.label or "", e.row_label) and e.take == strategy.take
                      and (strategy.role is None or e.role == strategy.role))
            elif strategy.kind == "table_cell":
                ok = (names_match(strategy.column or "", e.column_header) and e.row_key == strategy.row_key
                      and (strategy.role is None or e.role == strategy.role))
            elif strategy.kind == "scoped":
                ok = (e.role == strategy.role and strategy.within_role == "columnheader"
                      and names_match(strategy.within_name or "", e.column_header))
            elif strategy.kind == "css":
                ok = e.css == strategy.css
            else:
                ok = False
            if ok:
                out.append(e)
        return out

    async def same_element(self, a, b) -> bool:
        return a is b

    async def fingerprint(self, element: El) -> Fingerprint:
        return Fingerprint(role=element.role, tag=element.tag, input_type=element.input_type,
                           row_label=element.row_label, column_header=element.column_header)

    async def read_text(self, element: El) -> str:
        return element.text or self.typed.get(element.name or "", "") or element.name or ""

    def _allow(self, request: ActionRequest) -> PolicyDecision:
        decision = self.guard.check(request)
        if not decision.allowed:
            raise PolicyDenied(decision)
        return decision

    async def click(self, element: El, request: ActionRequest) -> PolicyDecision:
        decision = self._allow(request)
        self.log.append(f"click {element.role}")
        if self.hang_on_click:
            await asyncio.sleep(3600)
        if element.on_click:
            element.on_click()
        return decision

    async def type(self, element: El, text: str, request: ActionRequest) -> PolicyDecision:
        decision = self._allow(request)
        self.log.append(f"type {element.name}")
        self.typed[element.name or ""] = text
        return decision

    async def select(self, element: El, option: str, request: ActionRequest) -> PolicyDecision:
        return self._allow(request)

    async def press(self, key: str, request: ActionRequest) -> PolicyDecision:
        decision = self._allow(request)
        self.log.append(f"press {key}")
        if key == "Enter" and "Filter" in self.typed:  # submits the list filter
            self._goto(f"{BASE}/societe/list.php?leftmenu=customers&type=c&search_code={self.typed['Filter']}")
        return decision

    async def navigate(self, url: str, request: ActionRequest) -> PolicyDecision:
        decision = self._allow(request)
        self.log.append("navigate")
        self._goto(url)
        return decision

    async def masked_screenshot(self, masks, *, generic: bool) -> bytes:  # pragma: no cover
        return b""

    def describe(self) -> dict:
        return {"kind": "fake"}
