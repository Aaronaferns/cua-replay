"""Spike S7: normalized-exact name matching over accessible names captured from Dolibarr 23.0.4.

Reads the AI snapshots captured by the target spike (spikes/dolibarr/out/, git-ignored, regenerate with
s1_snapshot.py / s2_flows.py / s3_search_payment.py) and checks that the proposed rule

    normalize(s) = casefold(collapse_ws(strip_icon_glyphs(nfkc(s).replace(NBSP, " ")))).rstrip(":*").strip()
    match(wanted, name) = normalize(wanted) == normalize(name)          # anchored, never substring

hits each probe label exactly once and never its traps. Prints a table; exits 1 on any failure.

    uv run python spikes/s7_name_match.py
"""

from __future__ import annotations

import pathlib
import re
import sys
import unicodedata

OUT = pathlib.Path(__file__).parent / "dolibarr" / "out"
NAME_RE = re.compile(r'^\s*-\s+(?P<role>[a-z]+)\s+"(?P<name>(?:[^"\\]|\\.)*)"')


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).replace(" ", " ")
    # Icon fonts (Font Awesome in Dolibarr) render as private-use code points; they are not text.
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Co")
    s = re.sub(r"\s+", " ", s).strip().casefold()
    return s.rstrip(":*").strip()


def names(path: pathlib.Path) -> list[tuple[str, str]]:
    found = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = NAME_RE.match(line)
        if m:
            found.append((m.group("role"), m.group("name").encode().decode("unicode_escape", "ignore")
                          if "\\" in m.group("name") else m.group("name")))
    return found


# (snapshot, role, wanted label as an author would write it, expected number of matches)
PROBES = [
    ("04_customer_card", "generic", "Current outstanding bill", 1),
    ("04_customer_card", "generic", "current outstanding bill:", 1),       # case + trailing colon
    ("04_customer_card", "generic", "Current outstanding bill", 1),   # NBSP
    ("04_customer_card", "generic", "Current outstanding bill late", 1),
    ("04_customer_card", "generic", "outstanding bill", 0),                # substring must not match
    ("04_customer_card", "cell", "Customer Code", 1),
    ("04_customer_card", "cell", "Customer Code *", 1),                    # trailing required-marker
    ("04_customer_card", "cell", "Code", 0),
    ("04_customer_card", "link", "Current outstanding bill", 0),           # link name carries the amount + glyph
]


def main() -> int:
    if not OUT.exists():
        print(f"no captured snapshots in {OUT}; run the dolibarr spike scripts first")
        return 2
    failures = 0
    print(f"{'snapshot':20} {'role':8} {'wanted':34} {'hits':>4} {'expect':>6}  result")
    for snap, role, wanted, expect in PROBES:
        pool = names(OUT / f"{snap}.snapshot.txt")
        hits = [n for r, n in pool if r == role and normalize(n) == normalize(wanted)]
        substring_hits = [n for r, n in pool if r == role and normalize(wanted) in normalize(n)]
        ok = len(hits) == expect
        failures += not ok
        print(f"{snap:20} {role:8} {wanted!r:34} {len(hits):>4} {expect:>6}  {'PASS' if ok else 'FAIL'}"
              f"   (substring matching would hit {len(substring_hits)})")
    # The glyph-stripping claim: the link name normalizes to label + amount, never to the label alone.
    link = [n for r, n in names(OUT / "04_customer_card.snapshot.txt") if r == "link" and "outstanding" in n]
    print("link names normalize to:", [normalize(n) for n in link])
    print("S7", "PASS" if failures == 0 else f"FAIL ({failures})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
