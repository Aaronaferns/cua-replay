"""Lets compiler tests reuse the hand-built sample documents (tests/artifact)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "artifact"))
