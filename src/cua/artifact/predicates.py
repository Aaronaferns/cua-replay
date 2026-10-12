"""The closed predicate language used by state signatures, step checks and success checks.

Seven forms and nothing else:

    url_matches{pattern}                    path+query glob; '*' is the only wildcard; origin ignored
    text{equals | pattern, role?, frame?}           a node whose name or text equals (normalized-exact) or fully matches
                                            an anchored pattern (case-insensitive, over normalized text)
    target_visible{target}                  the capability target resolves to exactly one element
    target_equals_input{target, input, normalizer}
                                            the target's text equals the input value
    all{of}, any{of}, not{of}

`url_matches` and `text` are *static*: they read only one observed screen, so they can be evaluated by a pure,
context-free classifier. The two target forms need a live surface and the run's inputs; the replay interpreter
evaluates them. State signatures may use static forms only (checked by `cua.artifact.validate`).

Leaf module: pydantic and the standard library only.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cua.artifact.names import names_match, normalize_name


class _P(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class UrlMatches(_P):
    kind: Literal["url_matches"] = "url_matches"
    pattern: str = Field(min_length=1)  # e.g. "/societe/list.php*"; matched against path + "?" + query


class Text(_P):
    kind: Literal["text"] = "text"
    equals: str | None = None  # normalized-exact against the node's name, else its text
    pattern: str | None = None  # anchored (full match) over the normalized name or text, case-insensitive
    role: str | None = None  # restrict to nodes with this role ("text" = bare text nodes)
    frame: str | None = None  # restrict to nodes of this frame (as named by the surface)

    @model_validator(mode="after")
    def _one_matcher(self) -> Text:
        if (self.equals is None) == (self.pattern is None):
            raise ValueError("text needs exactly one of 'equals' or 'pattern'")
        if self.equals is not None and not normalize_name(self.equals):
            raise ValueError("text.equals is empty after normalization")
        if self.pattern is not None:
            try:
                _compile(self.pattern)
            except re.error:
                raise ValueError("text.pattern is not a valid regular expression") from None
        return self


class TargetVisible(_P):
    kind: Literal["target_visible"] = "target_visible"
    target: str


class TargetEqualsInput(_P):
    kind: Literal["target_equals_input"] = "target_equals_input"
    target: str
    input: str
    # "text": normalized-exact (case, spacing, icon glyphs, trailing ':' ignored); "exact": whitespace-trimmed equality
    normalizer: Literal["text", "exact"] = "text"


class AllOf(_P):
    kind: Literal["all"] = "all"
    of: list[Predicate] = Field(min_length=1)


class AnyOf(_P):
    kind: Literal["any"] = "any"
    of: list[Predicate] = Field(min_length=1)


class Not(_P):
    kind: Literal["not"] = "not"
    of: Predicate


Predicate = Annotated[
    UrlMatches | Text | TargetVisible | TargetEqualsInput | AllOf | AnyOf | Not,
    Field(discriminator="kind"),
]

for _m in (AllOf, AnyOf, Not):
    _m.model_rebuild()

STATIC_KINDS = frozenset({"url_matches", "text", "all", "any", "not"})


def walk(p: Predicate) -> Iterator[Predicate]:
    """Every predicate node, depth first, in declaration order."""
    yield p
    if isinstance(p, AllOf | AnyOf):
        for child in p.of:
            yield from walk(child)
    elif isinstance(p, Not):
        yield from walk(p.of)


def is_static(p: Predicate) -> bool:
    return all(node.kind in STATIC_KINDS for node in walk(p))


# --- static evaluation over one screen -------------------------------------------------------------------------


@dataclass(frozen=True)
class ScreenNode:
    """One node of an observed screen, surface-neutral: a role, an accessible name and/or visible text."""

    role: str
    name: str | None = None
    text: str | None = None
    frame: str | None = None


@dataclass(frozen=True)
class ScreenView:
    """What static predicates read: the URL and the nodes of one layer (page content or dialog)."""

    url: str
    nodes: tuple[ScreenNode, ...]


@lru_cache(maxsize=512)
def _compile(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE | re.DOTALL)


@lru_cache(maxsize=512)
def _glob(pattern: str) -> re.Pattern[str]:
    return re.compile(".*".join(re.escape(part) for part in pattern.split("*")), re.DOTALL)


def url_path(url: str) -> str:
    """Path plus query, origin dropped: profiles describe routes, and hosts differ between environments."""
    parts = urlsplit(url)
    path = parts.path or "/"
    return f"{path}?{parts.query}" if parts.query else path


def glob_match(pattern: str, value: str) -> bool:
    return _glob(pattern).fullmatch(value) is not None


def _text_holds(p: Text, view: ScreenView) -> bool:
    for node in view.nodes:
        if p.role is not None and node.role != p.role:
            continue
        if p.frame is not None and node.frame != p.frame:
            continue
        for value in (node.name, node.text):
            if not value:
                continue
            if p.equals is not None and names_match(p.equals, value):
                return True
            if p.pattern is not None and _compile(p.pattern).fullmatch(normalize_name(value)):
                return True
    return False


def holds(p: Predicate, view: ScreenView) -> bool:
    """Evaluate a static predicate. Target predicates are not static; asking for one here is a programming error."""
    if isinstance(p, UrlMatches):
        return glob_match(p.pattern, url_path(view.url))
    if isinstance(p, Text):
        return _text_holds(p, view)
    if isinstance(p, AllOf):
        return all(holds(c, view) for c in p.of)
    if isinstance(p, AnyOf):
        return any(holds(c, view) for c in p.of)
    if isinstance(p, Not):
        return not holds(p.of, view)
    raise TypeError(f"{p.kind} needs a live surface; it is not a static predicate")


def clause_score(p: Predicate, view: ScreenView) -> tuple[int, int]:
    """(satisfied, total) top-level clauses of a signature; used to report near misses for unknown screens."""
    clauses = p.of if isinstance(p, AllOf) else [p]
    return sum(holds(c, view) for c in clauses), len(clauses)
