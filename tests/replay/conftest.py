"""Lets replay tests reuse the sample documents (tests/artifact) and the hand-built trace (tests/compiler)."""

from __future__ import annotations

import sys
from pathlib import Path

for sibling in ("artifact", "compiler"):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / sibling))
