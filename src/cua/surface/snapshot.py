"""Parsing and shaping Playwright AI snapshots (`aria_snapshot(mode="ai")`).

The AI snapshot is YAML: one node per line, `- role "name" [ref=e12] [cursor=pointer]: inline text`, with keys
single-quoted when they contain YAML-special text, names JSON-escaped, and `- /url: ...` / `- text: ...` pseudo
nodes. This module is pure text processing: no browser, unit-tested on captured Dolibarr snapshots.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from cua.surface.names import clean

_REF = re.compile(r"\[ref=([^\]\s]+)\]")
_REF_MARK = re.compile(r" ?\[ref=[^\]\s]+\]")
_ATTR =re.compile(r"\[([a-z-]+)(?:=([^\]]*))?\]")
# Dolibarr (and most PHP apps) carry a CSRF token in links; the model never needs it and evidence must not keep it.
_URL_TOKEN = re.compile(r"(?i)\b(token|_token|csrf[_-]?token|sessid|phpsessid)=([^&\s\"']+)")


@dataclass(frozen=True)
class Node:
    depth: int
    role: str  # "cell", "link", ...; pseudo nodes are "text" and "/url"
    name: str | None = None
    ref: str | None = None
    attrs: dict[str, str | None] = field(default_factory=dict)
    value: str | None = None  # inline text after the colon (e.g. a textbox's value, a generic's text)
    line_no: int = 0


def _unquote_yaml_single(body: str) -> tuple[str, str]:
    """`'key ''x''': rest` -> ("key 'x'", ": rest")."""
    out, i = [], 1
    while i < len(body):
        ch = body[i]
        if ch == "'":
            if i + 1 < len(body) and body[i + 1] == "'":
                out.append("'")
                i += 2
                continue
            return "".join(out), body[i + 1 :]
        out.append(ch)
        i += 1
    return "".join(out), ""


def _split_key(body: str) -> tuple[str, str | None]:
    """Split `key: value` at the first colon outside a double-quoted name."""
    if body.startswith("'"):
        key, rest = _unquote_yaml_single(body)
    elif body.startswith('"'):
        end = _end_of_json_string(body, 0)
        try:
            key = json.loads(body[: end + 1])
        except ValueError:
            key = body[1:end]
        rest = body[end + 1 :]
    else:
        in_str, esc, cut = False, False, len(body)
        for i, ch in enumerate(body):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == ":" and (i + 1 == len(body) or body[i + 1] == " "):
                cut = i
                break
        key, rest = body[:cut], body[cut:]
    rest = rest.strip()
    if rest.startswith(":"):
        value = rest[1:].strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            try:
                value = json.loads(value)
            except ValueError:
                pass
        elif len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1].replace("''", "'")
        return key, (value or None)
    return key, None


def _end_of_json_string(s: str, start: int) -> int:
    esc = False
    for i in range(start + 1, len(s)):
        ch = s[i]
        if esc:
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == '"':
            return i
    return len(s) - 1


def parse_line(line: str, line_no: int = 0) -> Node | None:
    stripped = line.lstrip(" ")
    if not stripped.startswith("- "):
        return None
    depth = (len(line) - len(stripped)) // 2
    key, value = _split_key(stripped[2:])
    key = key.strip()
    if key.startswith("/"):
        return Node(depth=depth, role=key, value=value, line_no=line_no)
    role, _, rest = key.partition(" ")
    name = None
    rest = rest.strip()
    if rest.startswith('"'):
        end = _end_of_json_string(rest, 0)
        try:
            name = json.loads(rest[: end + 1])
        except ValueError:
            name = rest[1:end]
        rest = rest[end + 1 :]
    attrs = {m.group(1): m.group(2) for m in _ATTR.finditer(rest)}
    ref = attrs.pop("ref", None)
    return Node(depth=depth, role=role, name=name, ref=ref, attrs=attrs, value=value, line_no=line_no)


def parse(text: str) -> list[Node]:
    nodes = []
    for i, line in enumerate(text.splitlines()):
        node = parse_line(line, i)
        if node is not None:
            nodes.append(node)
    return nodes


def refs(text: str) -> list[str]:
    return _REF.findall(text)


def first_line_node(text: str) -> Node | None:
    """The element's own node in a `depth=0` element snapshot."""
    return next(filter(None, map(parse_line, text.splitlines())), None)


def link_names(text: str) -> list[str]:
    """Distinct, cleaned link names in document order (menu summaries)."""
    seen, out = set(), []
    for node in parse(text):
        if node.role == "link":
            name = clean(node.name)
            if name and name not in seen:
                seen.add(name)
                out.append(name)
    return out


def first_heading(text: str) -> str | None:
    for node in parse(text):
        if node.role == "heading" and clean(node.name or node.value):
            return clean(node.name or node.value)
    return None


def has_empty_frame(text: str) -> bool:
    """An iframe node without children: Playwright swallows child-frame errors and returns it empty."""
    nodes = parse(text)
    for i, node in enumerate(nodes):
        if node.role == "iframe":
            nxt = nodes[i + 1] if i + 1 < len(nodes) else None
            if nxt is None or nxt.depth <= node.depth:
                return True
    return False


def mask_url_tokens(text: str) -> str:
    """Replace CSRF/session token values in URLs with `***` (the model never needs them)."""
    return _URL_TOKEN.sub(lambda m: f"{m.group(1)}=***", text)


def strip_refs(text: str) -> str:
    return _REF_MARK.sub("", text)


def signature(*parts: str | None) -> str:
    """A screen signature over text parts with refs removed (refs change when a page re-renders). Field values stay
    in: the AI snapshot shows a textbox's value inline, so filling a form changes the signature."""
    h = hashlib.sha256()
    for part in parts:
        h.update(strip_refs(part or "").encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]
