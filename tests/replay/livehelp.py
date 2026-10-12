"""Shared helpers for tests that run against the live target (fixtures path only: never imported by replay)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "target" / "dolibarr"))

import dolictl  # noqa: E402
import seed  # noqa: E402
from dolirest import Rest, customer_by_code, outstanding_of  # noqa: E402
from livetarget import reset_to_baseline  # noqa: E402

from cua.config import settings  # noqa: E402


def baseline_fixtures():
    """Reset the target to the baseline. reset.sh exits 4 when the restore finished but overran its 15 s budget (a loaded
    host); the restore itself happened, so only the fixture check is repeated."""
    try:
        return reset_to_baseline()
    except dolictl.CtlError as e:
        if "budget" not in str(e):
            raise
        fixtures = seed.load_fixtures()
        admin = Rest.admin(settings())
        for c in fixtures.customers:
            assert outstanding_of(admin, customer_by_code(admin, c.code)["id"]) == c.outstanding
        return fixtures
