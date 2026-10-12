"""Locator candidates for one element, recorded on the engine replay will use.

For an element the agent acted on, try the ways replay could find it again, in rank order, through `surface.find()`:

    1  role + normalized-exact name
    2  same_row: the label cell of its row -> the control (or the value cell)
    3  table_cell: the column header (+ the row keyed by an input value, or the only such cell)
    4  scoped: role (+ name) inside the column header it sits under, or inside the open dialog
    5  the normalized name, ranked low
    6  css, last (stable attributes only)

A candidate is kept only if it finds exactly one element AND that element is the one the ref pointed at
(`surface.same_element`). Strings that contain an input's value are stored with its `{{name}}` token, so a draft never
holds a value; the search itself uses the real value. Nothing here knows Playwright: it speaks `Surface`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cua.artifact.names import normalize_name
from cua.artifact.schema import LOCATOR_FIELDS, Fingerprint, Strategy
from cua.compiler.trace import LocatorCandidate
from cua.surface.base import Element, Surface, SurfaceError

WHY = {
    1: "role and exact name",
    2: "the row's label cell leads to it",
    3: "its column header, in the row keyed by the input",
    4: "scoped by the container it sits in",
    5: "normalized name (low rank)",
    6: "stable attributes (structural fallback)",
}
MATCHED = "1 of 1, same element as ref"
_CELLISH = {"cell", "gridcell", "rowheader", "columnheader", "generic"}


def tokenize(text: str, inputs: Mapping[str, str]) -> str:
    """Replace each input value inside `text` with its `{{name}}` token (longest value first)."""
    for name, value in sorted(inputs.items(), key=lambda kv: -len(kv[1])):
        if value and value in text:
            text = text.replace(value, "{{" + name + "}}")
    return text


def _tokenized(strategy: Strategy, inputs: Mapping[str, str]) -> Strategy:
    update: dict[str, Any] = {}
    for f in LOCATOR_FIELDS:
        v = getattr(strategy, f)
        if isinstance(v, str) and (t := tokenize(v, inputs)) != v:
            update[f] = t
    return strategy.model_copy(update=update) if update else strategy


def _strategy(rank: int, kind: str, **fields: Any) -> Strategy:
    return Strategy(kind=kind, why=WHY[rank], matched_at_record=MATCHED, **fields)  # type: ignore[arg-type]


def proposals(
    *, role: str, name: str | None, fingerprint: Fingerprint, inputs: Mapping[str, str], dialog: str | None,
    css: list[str],
) -> list[tuple[int, Strategy]]:
    """The strategies worth trying, in rank order. Pure: no surface involved."""
    out: list[tuple[int, Strategy]] = []
    named = bool(name and name.strip())
    role_ok = role not in ("", "generic", "text")
    if named and role not in ("", "text"):  # a named generic (a `title`/`aria-label` box) is found by that name
        out.append((1, _strategy(1, "role", role=role, name=name)))
    if fingerprint.row_label:
        if fingerprint.tag in ("td", "th"):
            out.append((2, _strategy(2, "same_row", label=fingerprint.row_label, take="value")))
        if role_ok and role not in _CELLISH:
            out.append((2, _strategy(2, "same_row", label=fingerprint.row_label, take="control", role=role)))
    if fingerprint.column_header:
        col = fingerprint.column_header
        cell_role = None if role in _CELLISH or not role_ok else role
        for value_name in inputs:
            out.append((3, _strategy(3, "table_cell", column=col, row_key="{{" + value_name + "}}", role=cell_role)))
        out.append((3, _strategy(3, "table_cell", column=col, role=cell_role)))
        if role_ok:
            if named:
                out.append((4, _strategy(4, "scoped", role=role, name=name, within_role="columnheader", within_name=col)))
            out.append((4, _strategy(4, "scoped", role=role, within_role="columnheader", within_name=col)))
    if dialog and role_ok:
        if named:
            out.append((4, _strategy(4, "scoped", role=role, name=name, within_role="dialog", within_name=dialog)))
        out.append((4, _strategy(4, "scoped", role=role, within_role="dialog", within_name=dialog)))
    if named and role_ok and normalize_name(name or "") != (name or ""):
        out.append((5, _strategy(5, "role", role=role, name=normalize_name(name or ""))))
    for selector in css:
        out.append((6, _strategy(6, "css", css=selector)))
    return out


def _materialize(strategy: Strategy, inputs: Mapping[str, str]) -> Strategy:
    """The searchable form: `{{name}}` tokens replaced by the real values."""
    update: dict[str, Any] = {}
    for f in LOCATOR_FIELDS:
        v = getattr(strategy, f)
        if isinstance(v, str) and "{{" in v:
            for name, value in inputs.items():
                v = v.replace("{{" + name + "}}", value)
            update[f] = v
    return strategy.model_copy(update=update) if update else strategy


async def record_candidates(
    surface: Surface,
    element: Element,
    *,
    role: str,
    name: str | None,
    fingerprint: Fingerprint,
    inputs: Mapping[str, str],
    dialog: str | None = None,
) -> list[LocatorCandidate]:
    """Candidates that find exactly the element, in rank order. `find()` invalidates the refs of the current
    observation, so callers resolve every ref they need into elements before calling this."""
    css: list[str] = []
    hints = getattr(surface, "css_hints", None)
    if hints is not None:
        try:
            css = await hints(element)
        except SurfaceError:
            css = []
    kept: list[LocatorCandidate] = []
    seen: set[str] = set()
    for rank, proposed in proposals(role=role, name=name, fingerprint=fingerprint, inputs=inputs, dialog=dialog, css=css):
        stored = _tokenized(proposed, inputs)
        key = stored.model_dump_json()
        if key in seen:
            continue
        seen.add(key)
        try:
            found = await surface.find(_materialize(proposed, inputs))
        except (SurfaceError, ValueError):
            continue
        if len(found) != 1:
            continue
        if not await surface.same_element(found[0], element):
            continue
        kept.append(LocatorCandidate(rank=rank, strategy=stored, match_count=1, same_element=True))
    return kept
