"""Accessible-name and display-text normalization: one rule for classification, resolution and identity checks.

The rule comes from spike S7 (captured Dolibarr names): NFKC, NBSP folded to a space, icon-font glyphs dropped
(private-use code points), whitespace collapsed, casefolded, trailing ':' / '*' removed. Matching is anchored
equality of the normalized forms, never a substring test, so "Current outstanding bill" can never match
"Current outstanding bill late".

Leaf module: standard library only.
"""

from __future__ import annotations

import re
import unicodedata

_WS = re.compile(r"\s+")


def display_text(s: str) -> str:
    """Text as a person reads it: NFKC, NBSP -> space, icon glyphs dropped, whitespace collapsed. Case is kept."""
    s = unicodedata.normalize("NFKC", s).replace("\u00a0", " ")
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Co")
    return _WS.sub(" ", s).strip()


def normalize_name(s: str) -> str:
    """Comparison key for names and labels: `display_text`, casefolded, trailing ':' / '*' removed."""
    return display_text(s).casefold().rstrip(":*").strip()


def names_match(wanted: str, actual: str | None) -> bool:
    """Anchored, normalized equality. An empty wanted name never matches (it would match every unlabeled node)."""
    if actual is None:
        return False
    key = normalize_name(wanted)
    return bool(key) and key == normalize_name(actual)


_JS_SPECIAL = set("\\^$.*+?()[]{}|/-")


def _escape(word: str) -> str:
    return "".join("\\" + ch if ch in _JS_SPECIAL else ch for ch in word)


def name_regex(wanted: str) -> re.Pattern[str]:
    """The same rule as a case-insensitive regex whose source is also valid JavaScript, for name engines that only
    accept a pattern (a browser's accessible-name filter). Equivalent to `names_match` for ordinary text; NFKC and
    casefold edge cases (ligatures, German sharp s) differ, and the caller re-checks with `names_match`."""
    words = normalize_name(wanted).split(" ")
    if words == [""]:
        raise ValueError("empty name")
    gap = r"[\s\uE000-\uF8FF]*"
    body = r"[\s\uE000-\uF8FF]+".join(_escape(w) for w in words)
    return re.compile(f"^{gap}{body}{gap}[:*]*{gap}$", re.IGNORECASE)
