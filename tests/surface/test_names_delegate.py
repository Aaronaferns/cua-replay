"""The surface's name functions are the canonical ones (spike S7): same answers on the S7 probes."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "artifact"))
from test_artifact_names import CARD, PROBES  # noqa: E402

from cua.artifact import names as canonical  # noqa: E402
from cua.artifact.screen import parse_outline  # noqa: E402
from cua.surface import names as surface  # noqa: E402


def _pool() -> list[tuple[str, str]]:
    return [(n.role, n.name) for n in parse_outline(CARD) if n.name]


@pytest.mark.parametrize("role,wanted,expected", PROBES)
def test_surface_agrees_with_canonical_on_s7_probes(role, wanted, expected):
    pool = [(r, n) for r, n in _pool() if r == role]
    via_surface = [n for _, n in pool if surface.same_name(wanted, n)]
    via_canonical = [n for _, n in pool if canonical.names_match(wanted, n)]
    # the regex is a prefilter; the canonical comparison has the last word
    via_pattern = [n for _, n in pool if surface.name_pattern(wanted).match(n) and surface.same_name(wanted, n)]
    assert via_surface == via_canonical == via_pattern
    assert len(via_surface) == expected


def test_forms_delegate_to_canonical():
    for text in ("  Customer Code : ", " Current outstanding bill", "Total *", ""):
        assert surface.fold_text(text) == (canonical.display_text(text) if text else "")
        assert surface.normalize(text) == (canonical.normalize_name(text) if text else "")
    assert surface.clean("Customer Code:") == "Customer Code"
    assert surface.clean(None) == ""
    assert surface.same_name("Current outstanding bill", "Current outstanding bill late") is False
