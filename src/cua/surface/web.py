"""The web surface: Playwright (async) behind the `Surface` protocol.

Perception is Playwright's AI snapshot, scoped to the app's main content area plus the dialog layer, so the model
sees what an operator works with and not 30k characters of menus. Acting by ref uses Playwright's `aria-ref=`
engine; finding by `Strategy` uses Playwright's role engine with normalized-exact names, re-checked against the
element's own AI-snapshot line, so observation, recording and replay share one accessible-name engine.

Facts verified against Playwright 1.63 while building this (see tests/surface):
- refs from a locator-scoped AI snapshot resolve with `page.locator("aria-ref=<ref>")`, and a ref whose element is
  gone counts 0 immediately (no auto-wait). Only the refs of the *latest* snapshot resolve: the next `aria_snapshot`
  call, on any locator, drops the earlier ones. So `observe()` takes the snapshot that carries the actionable refs
  last (the topmost dialog if one is open, else the main content), `element_for_ref()` turns a ref into a positional
  path at once (independent of the ref map), and `find()`, which snapshots to verify names, invalidates the refs of
  the current observation;
- `get_by_role("generic", name=...)` never matches, although the AI snapshot names a generic from its `title` /
  `aria-label` (Dolibarr's "Current outstanding bill"); generics are therefore prefiltered by title/label;
- while a native alert is pending, `aria_snapshot()` and `screenshot()` block, so native dialogs are checked first.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from importlib.metadata import version
from typing import Any, Awaitable, TypeVar

from playwright.async_api import Dialog, Locator, Page
from playwright.async_api import Error as PlaywrightError
from pydantic import BaseModel, ConfigDict

from cua.artifact.schema import Fingerprint, Strategy
from cua.safety.policy import ActionRequest, Guard, PolicyDecision, PolicyDenied
from cua.surface import snapshot as snap
from cua.surface.base import (
    NativeDialogOpen,
    NotUniqueError,
    Observation,
    PageFacts,
    StaleRefError,
    SurfaceError,
    UnknownRefError,
)
from cua.surface.names import clean, fold_text, name_pattern, same_name

T = TypeVar("T")

_REF_SHAPE = re.compile(r"^[a-z]*[0-9]*e[0-9]+$")
_EMAIL = re.compile(r"\S+@\S+\.\S+")
# Roles whose accessible name Playwright's role engine does not compute (ARIA prohibits naming them), while the AI
# snapshot still shows a name from `title` / `aria-label`.
_NAMED_FROM_CONTENT = {"cell", "columnheader", "rowheader", "gridcell"}
_NAME_PROHIBITED ={"generic", "paragraph", "presentation", "none", "term", "definition", "time", "code"}
# Keys the agent may press. Everything else (shortcuts, F-keys, modifier chords) is refused before the guard.
ALLOWED_KEYS = frozenset({
    "Enter", "Tab", "Shift+Tab", "Escape", "Backspace", "Delete", "Space",
    "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown",
})
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")


class WebScope(BaseModel):
    """Where the main content, the dialog layer and the menus are, per app. CSS selectors are surface configuration,
    never artifact content."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    main: str = 'main, [role="main"]'  # the whole page is observed when this does not match exactly one element
    dialogs: str = '[role="dialog"], [role="alertdialog"], dialog[open]'
    menus: tuple[str, ...] = ()  # containers summarized as link names only
    include_urls: bool = True  # keep `/url:` lines (CSRF tokens are always masked)


# Dolibarr 23 page layout: #id-top (top menu + user block), #id-left (left menu), #id-right (main content).
# jQuery UI dialogs are appended to <body> with role="dialog", outside #id-right.
DOLIBARR_SCOPE = WebScope(main="#id-right", menus=("#id-top", "#id-left"))


@dataclass(frozen=True)
class WebElement:
    """A handle to exactly one element: a positional path resolved when the element was found, so it does not depend
    on Playwright's latest-snapshot-only ref map. `role` / `name` are what the AI snapshot showed at that moment."""

    locator: Locator
    how: str  # for messages: "ref e12", "role cell" (never typed or extracted values)
    role: str = ""
    name: str | None = None


# The positional path of an element (tag + same-tag sibling index, from the root), as an XPath.
_JS_PATH = """(el) => {
  const parts = [];
  for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
    let i = 1;
    for (let s = n.previousElementSibling; s; s = s.previousElementSibling) if (s.tagName === n.tagName) i++;
    parts.unshift(n.tagName.toLowerCase() + '[' + i + ']');
  }
  return '/' + parts.join('/');
}"""


def _js_fingerprint() -> str:
    return """(el) => {
      const tag = el.tagName.toLowerCase();
      const inputType = tag === 'input' ? (el.getAttribute('type') || 'text').toLowerCase() : null;
      const cell = el.closest('td, th');
      const row = cell ? cell.parentElement : null;
      let rowLabel = null, columnHeader = null;
      const colIndex = (r, c) => { let i = 0; for (const x of r.cells) { if (x === c) return i; i += x.colSpan || 1; } return -1; };
      const cellAt = (r, col) => { let i = 0; for (const x of r.cells) { const s = x.colSpan || 1; if (col >= i && col < i + s) return x; i += s; } return null; };
      if (row && row.tagName === 'TR') {
        const table = row.closest('table');
        const rows = table ? Array.from(table.rows) : [];
        const header = rows.find(r => Array.from(r.cells).some(c => c.tagName === 'TH'));
        if (header) {
          if (header !== row) { const h = cellAt(header, colIndex(row, cell)); columnHeader = h ? h.innerText : null; }
        } else {
          const first = row.cells[0];
          rowLabel = first ? first.innerText : null;
        }
      }
      return {tag, inputType, rowLabel, columnHeader};
    }"""


_JS_READ = """(el) => {
  if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) return el.value;
  if (el instanceof HTMLSelectElement) return el.selectedOptions.length ? el.selectedOptions[0].text : '';
  return el.innerText;
}"""

# Is `el` in the table column headed by header cell `h` (same table, colspan-aware)?
_JS_IN_COLUMN = """(el, h) => {
  const hrow = h.parentElement, table = hrow.closest('table');
  let col = 0; for (const x of hrow.cells) { if (x === h) break; col += x.colSpan || 1; }
  const cell = el.closest('td, th');
  if (!cell || cell === h) return false;
  const row = cell.parentElement;
  if (!row || row.closest('table') !== table) return false;
  let i = 0;
  for (const x of row.cells) { const s = x.colSpan || 1; if (x === cell) return col >= i && col < i + s; i += s; }
  return false;
}"""

# Selectors from attributes that survive a reload. Ids that look generated (digits, hashes, widget prefixes) are
# rejected; so are names with brackets. Each remaining class is one candidate (a banner ref has no id or name).
_JS_CSS_HINTS = r"""(el) => {
  const tag = el.tagName.toLowerCase(), out = [];
  const generated = (v) => /\d{3,}|[0-9a-f]{8,}|^(select2|ui-|ext-|react|:r|dol)/i.test(v);
  const esc = (v) => v.replace(/(["\\])/g, '\\$1');
  const id = el.getAttribute('id');
  if (id && !generated(id)) out.push(tag + '#' + CSS.escape(id));
  const name = el.getAttribute('name');
  if (name && !/[\[\]]/.test(name) && !generated(name)) {
    const type = el.getAttribute('type');
    out.push(tag + '[name="' + esc(name) + '"]' + (tag === 'input' && type ? '[type="' + esc(type) + '"]' : ''));
  }
  const cls = Array.from(el.classList).filter((c) => !generated(c) && /^[A-Za-z][\w-]*$/.test(c));
  for (const c of cls) out.push(tag + '.' + c);  // a class shared with other elements fails the uniqueness check
  return out;
}"""

_JS_IN_PLAIN_ROW = """(el) => !el.closest('tr').querySelector('a, input, select, textarea, button')"""

_COMMIT_LOAD_MS = 60_000
_ROWS = "xpath=./tr | ./thead/tr | ./tbody/tr | ./tfoot/tr"


def _safe_error(op: str, e: BaseException) -> SurfaceError:
    """Playwright messages can quote locators, URLs and call logs; keep only a category."""
    msg = str(e)
    if "Timeout" in msg or isinstance(e, asyncio.TimeoutError):
        kind = "timed out"
    elif "detached" in msg or "not attached" in msg:
        kind = "element detached"
    elif "not visible" in msg:
        kind = "element not visible"
    elif m := re.search(r"net::ERR_[A-Z_]+", msg):
        kind = m.group(0)
    else:
        kind = "failed"
    return SurfaceError(f"{op}: {kind}")


class WebSurface:
    """One live page. Construct it once per page; every action goes through the injected guard first."""

    def __init__(
        self,
        page: Page,
        guard: Guard,
        *,
        scope: WebScope = WebScope(),
        action_timeout_ms: int = 10_000,
        load_timeout_ms: int = 20_000,
        ref_timeout_ms: int = 500,
    ) -> None:
        self._page = page
        self._guard = guard
        self._scope = scope
        self._action_timeout = action_timeout_ms
        self._load_timeout = load_timeout_ms
        self._ref_timeout = ref_timeout_ms / 1000
        self._epoch = 0
        self._refs: frozenset[str] = frozenset()
        self._nodes: dict[str, snap.Node] = {}
        self._refs_dropped = False
        self._native: Dialog | None = None
        self._native_event = asyncio.Event()
        page.on("dialog", self._on_native_dialog)

    # -- state -------------------------------------------------------------------------------------------------

    @property
    def page(self) -> Page:
        return self._page

    @property
    def epoch(self) -> int:
        return self._epoch

    def _on_native_dialog(self, dialog: Dialog) -> None:
        # Held, never accepted: the page stays blocked until someone decides (a person, or a declared handler
        # calling `dismiss_native_dialog`). Registering this listener also stops Playwright's auto-dismiss.
        self._native = dialog
        self._native_event.set()

    async def _race_native(self, aw: Awaitable[T]) -> T:
        """Run a page operation, but stop waiting as soon as a native dialog blocks the page."""
        if self._native is not None:
            raise NativeDialogOpen("a native dialog is blocking the page")
        task = asyncio.ensure_future(aw)
        waiter = asyncio.ensure_future(self._native_event.wait())
        try:
            done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
        if task in done:
            return task.result()
        # The operation stays blocked behind the dialog; collect its outcome later so nothing leaks.
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        raise NativeDialogOpen("a native dialog is blocking the page")

    # -- observation -------------------------------------------------------------------------------------------

    async def settle(self, *, interval_ms: int = 150, timeout_ms: int = 5000) -> bool:
        """The navigation fence, then stability: the load event has fired and the main content plus the dialog layer
        read the same on two polls at least `interval_ms` apart. Takes ref-less snapshots, so it does not disturb the
        current observation's refs more than any snapshot does (observe again afterwards, as `observe(settle=True)`
        does). Gives up quietly at `timeout_ms`: whether a screen that never settles is a failure is the classifier's
        decision, not the surface's."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_ms / 1000
        page = self._page
        try:
            if self._native is not None:
                return False
            remaining = max(deadline - loop.time(), 0.1)
            await self._race_native(page.wait_for_load_state("load", timeout=min(self._load_timeout, remaining * 1000)))
            previous: str | None = None
            while True:
                current = await self._race_native(self._stable_text())
                if previous is not None and current == previous:
                    return True
                previous = current
                if loop.time() + interval_ms / 1000 > deadline:
                    return False
                await asyncio.sleep(interval_ms / 1000)
        except (PlaywrightError, NativeDialogOpen):
            return False

    async def _stable_text(self) -> str:
        """Ref-less text of the main content and the dialog layer (what 'stable' is judged on)."""
        main = self._page.locator(self._scope.main)
        scoped = await main.count() == 1
        target = main if scoped else self._page.locator("body")
        text = await target.aria_snapshot(timeout=self._action_timeout)
        dialogs = self._page.locator(self._scope.dialogs).filter(visible=True)
        n = await dialogs.count()
        parts = [text]
        for i in range(n):
            parts.append(await dialogs.nth(i).aria_snapshot(timeout=self._action_timeout))
        return "\x00".join(parts)

    async def observe(self, *, screenshot: bool = True, settle: bool = False) -> Observation:
        if settle:
            await self.settle()
        self._epoch += 1
        self._refs = frozenset()
        self._refs_dropped = False
        self._nodes = {}
        page = self._page
        try:
            if self._native is None:
                try:
                    await self._race_native(page.wait_for_load_state("load", timeout=self._load_timeout))
                except PlaywrightError:
                    pass  # observe what is there; settledness is the classifier's call, not the surface's
                # Only the latest snapshot's refs resolve, so the snapshot that carries the actionable refs is the
                # last one taken: menus first (names only), then the main content, then the topmost dialog.
                main = page.locator(self._scope.main)
                scoped = await self._race_native(main.count()) == 1
                menus = await self._race_native(self._menu_summary()) if scoped else []
                main_text = await self._race_native(self._main_content(main, scoped))
                dialog_text, dialog_name = await self._race_native(self._dialog_layer())
                png = None
                if screenshot:
                    png = await self._race_native(page.screenshot(type="png", scale="css", timeout=self._action_timeout))
                title = await self._race_native(page.title())
        except NativeDialogOpen:
            pass
        if self._native is not None:
            facts = PageFacts(url=snap.mask_url_tokens(page.url), title="",
                              native_dialog=f"{self._native.type}: {fold_text(self._native.message)}")
            return Observation(epoch=self._epoch, facts=facts, snapshot="")

        # A dialog is modal in the apps we drive: with one open, only its refs are actionable and the main content is
        # shown for context without refs.
        actionable = dialog_text if dialog_text else main_text
        if dialog_text:
            main_text = snap.strip_refs(main_text)
        self._refs = frozenset(snap.refs(actionable))
        self._nodes = {n.ref: n for n in snap.parse(actionable) if n.ref}
        facts = PageFacts(
            url=snap.mask_url_tokens(page.url),
            title=title,
            dialog=dialog_name,
            frames_unavailable=snap.has_empty_frame(main_text) or snap.has_empty_frame(dialog_text or ""),
        )
        return Observation(
            epoch=self._epoch,
            facts=facts,
            snapshot=self._shape(main_text),
            dialog_snapshot=self._shape(dialog_text) if dialog_text else None,
            menu_summary=menus,
            screenshot_png=png,
        )

    def _shape(self, text: str) -> str:
        text = snap.mask_url_tokens(text)
        if not self._scope.include_urls:
            text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("- /url:"))
        return text

    async def _dialog_layer(self) -> tuple[str | None, str | None]:
        dialogs = self._page.locator(self._scope.dialogs).filter(visible=True)
        n = await dialogs.count()
        if n == 0:
            return None, None
        text = await dialogs.nth(n - 1).aria_snapshot(mode="ai", timeout=self._action_timeout)  # last = opened last
        top = snap.first_line_node(text)
        name = clean(top.name) if top and top.role in ("dialog", "alertdialog") and top.name else None
        return text, name

    async def _main_content(self, main: Locator, scoped: bool) -> str:
        if scoped:
            return await main.aria_snapshot(mode="ai", timeout=self._action_timeout)
        # No (single) main container, e.g. a login page: observe the whole page.
        return await self._page.aria_snapshot(mode="ai", timeout=self._action_timeout)

    async def _menu_summary(self) -> list[str]:
        names: list[str] = []
        for selector in self._scope.menus:
            menu = self._page.locator(selector)
            if await menu.count() != 1:
                continue
            for name in snap.link_names(await menu.aria_snapshot(mode="ai", timeout=self._action_timeout)):
                # menu entries are navigation labels; anything that looks like an e-mail address is user data
                if name not in names and not _EMAIL.search(name):
                    names.append(name)
        return names

    # -- refs and strategies -----------------------------------------------------------------------------------

    async def element_for_ref(self, ref: str, epoch: int) -> WebElement:
        if epoch != self._epoch:
            raise StaleRefError(f"ref {ref} is from observation {epoch}; the current observation is {self._epoch}")
        if ref not in self._refs or not _REF_SHAPE.match(ref):
            raise UnknownRefError(f"ref {ref} is not in the current observation")
        if self._refs_dropped:
            raise StaleRefError(f"ref {ref} was invalidated by a later find(); observe again")
        ref_locator = self._page.locator(f"aria-ref={ref}")
        try:
            n = await asyncio.wait_for(ref_locator.count(), timeout=self._ref_timeout)
            if n == 1:
                path = await asyncio.wait_for(ref_locator.evaluate(_JS_PATH), timeout=self._ref_timeout)
        except (asyncio.TimeoutError, PlaywrightError) as e:
            raise StaleRefError(f"ref {ref} could not be resolved; observe again") from e
        if n == 0:
            raise StaleRefError(f"ref {ref} no longer exists; observe again")
        if n > 1:
            raise NotUniqueError(f"ref {ref} matched {n} elements")
        node = self._nodes.get(ref)
        return WebElement(self._page.locator(f"xpath={path}"), f"ref {ref}", node.role if node else "",
                          node.name if node else None)

    async def find(self, strategy: Strategy) -> list[WebElement]:
        """Every element the strategy matches (callers enforce exactly one). Names are normalized-exact. Verifying
        names takes snapshots, so the refs of the current observation stop resolving: observe again for new refs."""
        self._refs_dropped = True
        kind = strategy.kind
        if kind == "role":
            if not strategy.role:
                raise ValueError("role strategy needs a role")
            return await self._verified(self._role_locator(self._page, strategy.role, strategy.name),
                                        strategy.role, strategy.name, f"role {strategy.role}")
        if kind == "same_row":
            return await self._same_row(strategy)
        if kind == "table_cell":
            return await self._table_cell(strategy)
        if kind == "scoped":
            return await self._scoped(strategy)
        if kind == "css":
            if not strategy.css:
                raise ValueError("css strategy needs css")
            return await self._verified(self._page.locator(strategy.css), strategy.role, None, "css")
        raise ValueError(f"unknown strategy kind {kind!r}")

    @staticmethod
    def _role_locator(root: Page | Locator, role: str, name: str | None) -> Locator:
        if name is None:
            return root.get_by_role(role)  # type: ignore[arg-type]
        rx = name_pattern(name)
        if role in _NAME_PROHIBITED:
            return root.get_by_title(rx).or_(root.get_by_label(rx))
        return root.get_by_role(role, name=rx)  # type: ignore[arg-type]

    async def _verified(self, candidates: Locator, role: str | None, name: str | None, how: str) -> list[WebElement]:
        """Re-check each prefiltered hit on its own AI-snapshot line (role + normalized-exact name), then pin it as a
        positional path right away (each snapshot drops the refs of the previous one)."""
        n = await candidates.count()
        out: list[WebElement] = []
        for i in range(n):
            nth = candidates.nth(i)
            try:
                line = await nth.aria_snapshot(mode="ai", depth=0, timeout=self._action_timeout)
                path = await nth.evaluate(_JS_PATH, timeout=self._action_timeout)
                node = snap.first_line_node(line)
                observed = node.name if node else None
                if node and observed is None and node.role in _NAMED_FROM_CONTENT:
                    # The AI snapshot leaves out a name that only repeats a child's (a header cell holding one
                    # sort link): the name is then the cell's own text.
                    observed = await nth.evaluate(_JS_READ, timeout=self._action_timeout)
            except PlaywrightError:
                continue
            if node is None:
                continue
            if role is not None and node.role != role:
                continue
            if name is not None and not same_name(name, observed):
                continue
            out.append(WebElement(self._page.locator(f"xpath={path}"), how, node.role, observed))
        return out

    async def _cells_named(self, name: str) -> list[WebElement]:
        rx = name_pattern(name)
        found = []
        for role in ("cell", "rowheader", "columnheader"):
            found += await self._verified(self._page.get_by_role(role, name=rx), role, name, role)  # type: ignore[arg-type]
        return found

    async def _same_row(self, s: Strategy) -> list[WebElement]:
        if s.label is None or s.take is None:
            raise ValueError("same_row strategy needs label and take")
        out: list[WebElement] = []
        for label in await self._cells_named(s.label):
            if s.take == "value":
                value = label.locator.locator("xpath=following-sibling::*[self::td or self::th][1]")
                out += await self._verified(value, None, None, "same_row value")
            else:
                if not s.role:
                    raise ValueError("same_row control needs a role")
                following = label.locator.locator("xpath=following-sibling::*[self::td or self::th]")
                out += await self._verified(self._role_locator(following, s.role, s.name), s.role, s.name,
                                            "same_row control")
        return out

    async def _in_column(self, candidates: list[WebElement], header: WebElement) -> list[WebElement]:
        handle = await header.locator.element_handle(timeout=self._action_timeout)
        return [c for c in candidates if await c.locator.evaluate(_JS_IN_COLUMN, handle)]

    async def _headers(self, column: str) -> list[WebElement]:
        found = await self._verified(self._page.get_by_role("columnheader", name=name_pattern(column)),
                                     "columnheader", column, "columnheader")
        if found:
            return found
        # A header row of plain cells (no <th> sort links): the cells named so in a row holding no control or link.
        for role in ("cell", "rowheader"):
            for h in await self._verified(self._page.get_by_role(role, name=name_pattern(column)), role, column, role):
                if await h.locator.evaluate(_JS_IN_PLAIN_ROW):
                    found.append(h)
        return found

    async def _has_key(self, cells: Locator, key: str) -> bool:
        """Does one of the row's own cells read as the key (a bare ref, or a link holding it)?"""
        for i in range(await cells.count()):
            if same_name(key, await self.read_text(WebElement(cells.nth(i), "row key"))):
                return True
        return False

    async def _table_cell(self, s: Strategy) -> list[WebElement]:
        """The cell in column `column` of the row keyed by `row_key` (or of every row, when the caller wants the table
        to have exactly one), or the `role` element inside that cell."""
        if s.column is None:
            raise ValueError("table_cell strategy needs column")
        out: list[WebElement] = []
        for header in await self._headers(s.column):
            table = header.locator.locator("xpath=ancestor::table[1]")
            rows = table.locator(_ROWS)
            if s.row_key is not None:
                rows = rows.filter(has_text=s.row_key)
            for i in range(await rows.count()):
                cells = rows.nth(i).locator("xpath=./td | ./th")
                if s.row_key is not None and not await self._has_key(cells, s.row_key):
                    continue  # the key sits in a nested table, not in a cell of this row
                in_column = await self._in_column(await self._verified(cells, None, None, "table_cell"), header)
                if s.role and s.role not in _NAMED_FROM_CONTENT and s.role != "generic":
                    for cell in in_column:
                        out += await self._verified(self._role_locator(cell.locator, s.role, None), s.role, None,
                                                    "table_cell")
                else:
                    out += in_column
        return out

    async def _scoped(self, s: Strategy) -> list[WebElement]:
        if not s.role or not s.within_role or s.within_name is None:
            raise ValueError("scoped strategy needs role, within_role and within_name")
        out: list[WebElement] = []
        if s.within_role == "columnheader":
            # A list filter: the control in the same column as the header (Dolibarr's filter row sits above it).
            for header in await self._headers(s.within_name):
                table = header.locator.locator("xpath=ancestor::table[1]")
                found = await self._verified(self._role_locator(table, s.role, s.name), s.role, s.name, "scoped")
                out += await self._in_column(found, header)
            return out
        containers = await self._verified(self._role_locator(self._page, s.within_role, s.within_name),
                                          s.within_role, s.within_name, s.within_role)
        for container in containers:
            out += await self._verified(self._role_locator(container.locator, s.role, s.name), s.role, s.name,
                                        "scoped")
        return out

    async def same_element(self, a: WebElement, b: WebElement) -> bool:
        try:
            handle = await b.locator.element_handle(timeout=self._action_timeout)
            if handle is None:
                return False
            return bool(await a.locator.evaluate("(x, y) => x === y", handle))
        except PlaywrightError:
            return False

    async def fingerprint(self, element: WebElement) -> Fingerprint:
        try:
            info = await element.locator.evaluate(_js_fingerprint(), timeout=self._action_timeout)
        except PlaywrightError as e:
            raise _safe_error("fingerprint", e) from None
        return Fingerprint(
            role=element.role,
            tag=info["tag"],
            input_type=info["inputType"],
            frame=None,  # main frame only: Dolibarr has no frames (frame handling is design-only)
            row_label=clean(info["rowLabel"]) or None,
            column_header=clean(info["columnHeader"]) or None,
        )

    async def read_text(self, element: WebElement) -> str:
        try:
            return fold_text(await self._race_native(element.locator.evaluate(_JS_READ)))
        except PlaywrightError as e:
            raise _safe_error("read_text", e) from None

    async def css_hints(self, element: WebElement) -> list[str]:
        try:
            return list(await element.locator.evaluate(_JS_CSS_HINTS, timeout=self._action_timeout))
        except PlaywrightError as e:
            raise _safe_error("css_hints", e) from None

    @staticmethod
    def accessible(element: WebElement) -> tuple[str, str | None]:
        """(role, name) as the AI snapshot showed them when the element was found."""
        return element.role, element.name

    # -- actions -----------------------------------------------------------------------------------------------

    def _authorize(self, request: ActionRequest, kind: str) -> PolicyDecision:
        if request.kind != kind:
            raise ValueError(f"{kind} called with a {request.kind} request")
        decision = self._guard.check(request)
        if not decision.allowed:
            raise PolicyDenied(decision)
        return decision

    async def _unique(self, element: WebElement) -> Locator:
        """The count()==1 precheck: before the guard (a stale element must not burn a single-use approval) and before
        the UI is touched. Never resolves to 'the first'."""
        try:
            n = await asyncio.wait_for(self._race_native(element.locator.count()), timeout=self._action_timeout / 1000)
        except (asyncio.TimeoutError, PlaywrightError) as e:
            raise StaleRefError(f"{element.how} could not be resolved; observe again") from e
        if n == 0:
            raise StaleRefError(f"{element.how} no longer exists; observe again")
        if n > 1:
            raise NotUniqueError(f"{element.how} matched {n} elements; refusing to act on it")
        return element.locator

    async def _act(self, op: str, aw: Awaitable[Any]) -> None:
        try:
            await self._race_native(aw)
        except (NativeDialogOpen, SurfaceError):
            raise
        except PlaywrightError as e:
            raise _safe_error(op, e) from None

    async def click(self, element: WebElement, request: ActionRequest) -> PolicyDecision:
        locator = await self._unique(element)
        decision = self._authorize(request, "click")
        if request.effect != "commit":
            await self._act("click", locator.click(timeout=self._action_timeout))
            return decision
        # A commit's POST may answer slowly: the click is done once it lands, so it never waits on the navigation and
        # a load that does not finish in time is not an error (the commit happened; it is never retried).
        await self._act("click", locator.click(timeout=self._action_timeout, no_wait_after=True))
        try:
            await self._race_native(self._page.wait_for_load_state("load", timeout=_COMMIT_LOAD_MS))
        except (asyncio.TimeoutError, PlaywrightError):
            pass
        return decision

    async def type(self, element: WebElement, text: str, request: ActionRequest) -> PolicyDecision:
        if _CONTROL.search(text):
            raise ValueError("control characters cannot be typed")
        locator = await self._unique(element)
        decision = self._authorize(request, "type")
        await self._act("type", locator.clear(timeout=self._action_timeout))
        await self._act("type", locator.press_sequentially(text, timeout=self._action_timeout))
        return decision

    async def select(self, element: WebElement, option: str, request: ActionRequest) -> PolicyDecision:
        locator = await self._unique(element)
        decision = self._authorize(request, "select")
        await self._act("select", locator.select_option(label=option, timeout=self._action_timeout))
        return decision

    async def press(self, key: str, request: ActionRequest) -> PolicyDecision:
        if key not in ALLOWED_KEYS:
            raise ValueError(f"key {key!r} is not allowed")
        decision = self._authorize(request, "press")
        await self._act("press", self._page.keyboard.press(key))
        return decision

    async def navigate(self, url: str, request: ActionRequest) -> PolicyDecision:
        if request.target_url != url:
            raise ValueError("navigate request must name the destination it authorizes")
        if not url.startswith(("http://", "https://")):
            raise ValueError("navigate accepts absolute http(s) URLs only")
        decision = self._authorize(request, "navigate")
        await self._act("navigate", self._page.goto(url, timeout=self._load_timeout, wait_until="load"))
        return decision

    async def dismiss_native_dialog(self, request: ActionRequest) -> PolicyDecision:
        """Cancel a pending native dialog (the safe answer; accepting is never automated). Guarded as a click."""
        if self._native is None:
            raise SurfaceError("no native dialog is open")
        decision = self._authorize(request, "click")
        dialog, self._native = self._native, None
        self._native_event.clear()
        await dialog.dismiss()
        return decision

    # -- evidence ----------------------------------------------------------------------------------------------

    async def masked_screenshot(self, masks: list[WebElement], *, generic: bool) -> bytes:
        locators = [m.locator for m in masks]
        if generic:
            locators.append(self._page.locator("input, select, textarea, td"))
        try:
            return await self._race_native(self._page.screenshot(
                type="png", scale="css", mask=locators, mask_color="#000000", timeout=self._action_timeout))
        except PlaywrightError as e:
            raise _safe_error("screenshot", e) from None

    def describe(self) -> dict[str, Any]:
        browser = self._page.context.browser
        return {
            "kind": "web",
            "engine": "playwright",
            "playwright": version("playwright"),
            "browser": f"{browser.browser_type.name} {browser.version}" if browser else None,
            "scope": self._scope.model_dump(),
        }
