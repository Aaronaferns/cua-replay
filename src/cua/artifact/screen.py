"""Reading a text outline of a screen into surface-neutral nodes.

Surfaces describe a screen as an indented outline, one node per line:

    - textbox "Login" [active] [ref=e15]
    - cell "Customer Code" [ref=e297]
    - generic [ref=e299]: CU1212-0005
    - text: Third party
    - link "Back to list" [ref=e265]:
      - /url: /societe/list.php

A line is `role`, an optional quoted accessible name, optional `[attributes]`, and an optional `: inline text`
(`text:` lines are bare text nodes). Attribute and `/url:` lines carry nothing a signature may depend on and are
skipped. Lines whose whole key is single-quoted (keys that contain a colon) are handled. Refs are ephemeral and
never read.

Leaf module: standard library only.
"""

from __future__ import annotations

import json
import re

from cua.artifact.predicates import ScreenNode, ScreenView

_KEY = re.compile(
    r'^(?P<role>[A-Za-z][\w-]*)'
    r'(?: "(?P<name>(?:[^"\\]|\\.)*)")?'
    r'(?P<attrs>(?: \[[^\]]*\])*)'
    r'(?P<tail>:.*)?$'
)


def _unquote_double(value: str) -> str:
    try:
        decoded = json.loads(value)
    except ValueError:
        return value
    return decoded if isinstance(decoded, str) else value


def _split_single_quoted(rest: str) -> tuple[str, str] | None:
    """`'key with: colon': value` -> (key, ': value'). The YAML escape for a quote inside is two quotes."""
    i = 1
    while i < len(rest):
        if rest[i] == "'":
            if rest[i + 1 : i + 2] == "'":
                i += 2
                continue
            return rest[1:i].replace("''", "'"), rest[i + 1 :]
        i += 1
    return None


def parse_outline(text: str, *, frame: str | None = None) -> tuple[ScreenNode, ...]:
    nodes: list[ScreenNode] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        rest = stripped[2:]
        if rest.startswith("/"):  # "/url: ..." and similar property lines
            continue
        tail = ""
        if rest.startswith("'"):
            split = _split_single_quoted(rest)
            if split is None:
                continue
            rest, tail = split
        m = _KEY.match(rest + tail)
        if m is None:
            continue
        role = m.group("role")
        name = m.group("name")
        name = _unquote_double(f'"{name}"') if name else None
        inline = (m.group("tail") or "")[1:].strip()
        if inline.startswith('"') and inline.endswith('"') and len(inline) >= 2:
            inline = _unquote_double(inline)
        if role == "text":
            if inline:
                nodes.append(ScreenNode(role="text", text=inline, frame=frame))
            continue
        nodes.append(ScreenNode(role=role, name=name, text=inline or None, frame=frame))
    return tuple(nodes)


def view_from_outline(url: str, outline: str, *, frame: str | None = None) -> ScreenView:
    return ScreenView(url=url, nodes=parse_outline(outline, frame=frame))
