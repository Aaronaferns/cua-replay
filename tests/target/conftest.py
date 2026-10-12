"""Make the target's helper modules (target/dolibarr/*.py, not an installed package) importable by tests."""

from __future__ import annotations

import sys
from pathlib import Path

TARGET_DIR = Path(__file__).resolve().parents[2] / "target" / "dolibarr"
if str(TARGET_DIR) not in sys.path:
    sys.path.insert(0, str(TARGET_DIR))
