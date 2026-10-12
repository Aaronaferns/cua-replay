"""Normalized-exact name matching (spike S7), shared by observation, locator synthesis and replay.

A name is matched anchored and case-insensitively after folding the noise legacy markup puts into accessible names:
NFKC, NBSP -> space, private-use icon glyphs dropped (icon fonts such as Font Awesome render as private-use code
points), whitespace collapsed, optional trailing ':' / '*' (field labels, required markers). Never substring: the
whole name must match, so "Current outstanding bill" never matches "Current outstanding bill late".

`name_pattern()` turns a wanted name into a regular expression for Playwright's own name engine. It is a prefilter:
callers re-check every hit with `same_name()` on the observed name, so the final decision always uses `normalize()`.
Names that differ only by NFKC compatibility characters (ligatures, full-width forms) are not matched by the
prefilter, which fails closed (no match), never wrong.
"""

from __future__ import annotations

import re
import unicodedata

_PUA = "\ue000-\uf8ff"
_WS = re.compile(r"\s+")
# Characters with a meaning in both Python and JavaScript regular expressions.
_REGEX_SPECIAL = set("\\^$.|?*+()[]{}/")


def fold_text(s: str | None) -> str:
    """Visible text as a person reads it: NFKC, NBSP folded, icon glyphs dropped, whitespace collapsed."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s).replace("\u00a0", " ")
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Co")
    return _WS.sub(" ", s).strip()


def clean(s: str | None) -> str:
    """Display form of a name: `fold_text()` with a trailing ':'/'*' removed. Case is kept, so stored names stay
    readable."""
    return fold_text(s).rstrip(":*").strip()


def normalize(s: str | None) -> str:
    """Comparison form: `clean()` plus casefold."""
    return clean(s).casefold()


def same_name(wanted: str, observed: str | None) -> bool:
    return normalize(wanted) == normalize(observed)


def _escape(text: str) -> str:
    return "".join("\\" + ch if ch in _REGEX_SPECIAL else ch for ch in text)


def name_pattern(wanted: str) -> re.Pattern[str]:
    """Anchored, case-insensitive pattern accepted by both Python `re` and JavaScript `RegExp`."""
    words = clean(wanted).split(" ")
    gap = f"[\\s{_PUA}]"
    body = f"{gap}+".join(_escape(w) for w in words if w)
    return re.compile(f"^{gap}*{body}{gap}*(?:[:*]{gap}*)*$", re.IGNORECASE)
