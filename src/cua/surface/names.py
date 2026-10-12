"""Name matching for the surface: a thin layer over the canonical normalizer `cua.artifact.names` (spike S7).

There is exactly one rule (NFKC, NBSP folded, icon glyphs dropped, whitespace collapsed, casefolded, trailing ':' / '*'
removed, anchored equality, never a substring). Observation, recording and replay all reach it through here or
directly; nothing in this module reimplements it.

`name_pattern()` is a prefilter for Playwright's own name engine, which only accepts a pattern. Callers re-check every
hit with `same_name()` on the observed name, so the final decision always uses the canonical comparison.
"""

from __future__ import annotations

import re

from cua.artifact.names import display_text, name_regex, names_match, normalize_name


def fold_text(s: str | None) -> str:
    """Visible text as a person reads it (case kept)."""
    return display_text(s) if s else ""


def clean(s: str | None) -> str:
    """Display form of a name: `fold_text()` with a trailing ':'/'*' removed. Case is kept, so stored names stay
    readable."""
    return fold_text(s).rstrip(":*").strip()


def normalize(s: str | None) -> str:
    """Comparison form (casefolded, trailing ':'/'*' removed)."""
    return normalize_name(s) if s else ""


def same_name(wanted: str, observed: str | None) -> bool:
    return names_match(wanted, observed)


def name_pattern(wanted: str) -> re.Pattern[str]:
    """Anchored, case-insensitive pattern accepted by both Python `re` and JavaScript `RegExp`."""
    return name_regex(wanted)
