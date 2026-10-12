"""Shared helpers for tests that run against the live target (fixtures path only: never imported by replay)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "target" / "dolibarr"))

from dolirest import Rest, customer_by_code, outstanding_of  # noqa: E402
from livetarget import reset_to_baseline  # noqa: E402

from cua.config import settings  # noqa: E402


def baseline_fixtures():
    """Reset the target to the baseline and verify the fixture customers (a slow restore only warns)."""
    return reset_to_baseline()
