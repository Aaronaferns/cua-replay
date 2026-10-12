"""The canonical name normalizer (spike S7): anchored, normalized-exact, never a substring test."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from cua.artifact.names import display_text, name_regex, names_match, normalize_name
from cua.artifact.screen import parse_outline

GLYPH = ""  # an icon-font code point (Font Awesome renders as private use)
NBSP = " "

# Accessible names as Dolibarr 23.0.4 exposes them on a customer card (labels only, no customer data).
CARD = f"""
- generic "Current outstanding bill" [ref=e456]:
  - text: Current outstanding bill
- generic "Current{NBSP}outstanding bill" [ref=e457]
- generic "Current outstanding bill late" [ref=e460]:
- link "{GLYPH} Current outstanding bill 75.37{NBSP}€" [ref=e455] [cursor=pointer]:
  - /url: /compta/recap-compta.php?socid=10
- link "{GLYPH} Current outstanding bill late 83.00{NBSP}€" [ref=e459]
- cell "Customer Code" [ref=e297]
- cell "Nature of Third party" [ref=e292]
"""

# (role, wanted label as an author would write it, expected number of matches)
PROBES = [
    ("generic", "Current outstanding bill", 2),  # exact + the NBSP variant normalize identically
    ("generic", "current outstanding bill:", 2),  # case + trailing colon
    ("generic", f"Current{NBSP}outstanding{NBSP}bill", 2),  # NBSP in the wanted label
    ("generic", "Current outstanding bill late", 1),
    ("generic", "outstanding bill", 0),  # a substring must not match
    ("cell", "Customer Code", 1),
    ("cell", "Customer Code *", 1),  # trailing required-marker
    ("cell", "Code", 0),
    ("link", "Current outstanding bill", 0),  # the link name carries the amount, so it is a different name
]


def _pool() -> list[tuple[str, str]]:
    return [(n.role, n.name) for n in parse_outline(CARD) if n.name]


@pytest.mark.parametrize("role,wanted,expected", PROBES)
def test_s7_probe(role: str, wanted: str, expected: int):
    hits = [name for r, name in _pool() if r == role and names_match(wanted, name)]
    assert len(hits) == expected


def test_substring_matching_would_have_hit_the_trap():
    wanted = "Current outstanding bill"
    substring = [n for r, n in _pool() if r == "generic" and normalize_name(wanted) in normalize_name(n)]
    anchored = [n for r, n in _pool() if r == "generic" and names_match(wanted, n)]
    assert len(substring) == len(anchored) + 1  # "... late" is the trap anchoring avoids


def test_normalization_steps():
    assert normalize_name(f"  {GLYPH} Customer{NBSP}  Code :* ") == "customer code"
    assert normalize_name("Ｃustomer Ｃode") == "customer code"  # NFKC folds full-width letters
    assert display_text(f"{GLYPH}Third{NBSP}party") == "Third party"
    assert display_text("Mixed   Case\tText") == "Mixed Case Text"  # display keeps case


def test_link_name_normalizes_to_label_plus_amount_never_the_label_alone():
    link = [n for r, n in _pool() if r == "link"][0]
    assert normalize_name(link) == "current outstanding bill 75.37 €"


def test_empty_wanted_name_never_matches():
    assert not names_match("", "")
    assert not names_match(" : ", "anything")
    assert not names_match("x", None)


@pytest.mark.parametrize("role,wanted,expected", PROBES)
def test_name_regex_agrees_with_names_match(role: str, wanted: str, expected: int):
    rx = name_regex(wanted)
    hits = [n for r, n in _pool() if r == role and rx.search(n)]
    assert len(hits) == expected
    assert all(names_match(wanted, n) for n in hits)


def test_name_regex_escapes_metacharacters():
    assert name_regex("Total (excl. tax)").search("total (excl. tax):")
    assert not name_regex("a.c").search("abc")


def test_name_regex_source_is_javascript_compatible():
    # no Python-only constructs: inline flags, named groups, possessive quantifiers
    src = name_regex("Customer Code *").pattern
    assert not re.search(r"\(\?[a-zA-Z<P]", src)


SPIKE_OUT = Path(os.environ.get("CUA_SPIKE_OUT") or Path(__file__).resolve().parents[2] / "spikes" / "dolibarr" / "out")


@pytest.mark.skipif(not (SPIKE_OUT / "04_customer_card.snapshot.txt").exists(),
                    reason="captured Dolibarr snapshots are local-only (spikes/dolibarr/out, or set CUA_SPIKE_OUT)")
def test_s7_probes_on_captured_snapshot():
    nodes = parse_outline((SPIKE_OUT / "04_customer_card.snapshot.txt").read_text(encoding="utf-8"))
    pool = [(n.role, n.name) for n in nodes if n.name]
    for role, wanted, _ in PROBES:
        hits = [n for r, n in pool if r == role and names_match(wanted, n)]
        # anchored matching never returns more than the exact label's own occurrences
        assert all(normalize_name(n) == normalize_name(wanted) for n in hits)
    assert [n for r, n in pool if r == "generic" and names_match("Current outstanding bill", n)]
    assert not [n for r, n in pool if r == "generic" and names_match("outstanding bill", n)]
