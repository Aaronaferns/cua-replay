"""The baseline every live test starts from: the seeded, known state of the target.

`reset_to_baseline()` restores the post-seed dump (creating it first, from `seed.py`, when none exists yet) and checks
that the fixture customers carry the outstanding amounts the fixtures promise. Tests marked `live` call it (through a
fixture) for every state they depend on.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dolictl  # noqa: E402
import seed  # noqa: E402
from dolirest import Fixtures, Rest, customer_by_code, outstanding_of  # noqa: E402

from cua.config import settings  # noqa: E402


class StaleBaseline(Exception):
    pass


def reset_to_baseline() -> Fixtures:
    cfg = settings()
    if not dolictl.DUMP_FILE.exists():
        fixtures = seed.seed(cfg)
        dolictl.run_script("reset.sh", "dump")
    else:
        dolictl.run_script("reset.sh")  # a slow restore only warns; the fixture check below is the real test
        fixtures = seed.load_fixtures()
    admin = Rest.admin(cfg)
    for customer in fixtures.customers:
        found = customer_by_code(admin, customer.code)
        if found is None or outstanding_of(admin, found["id"]) != customer.outstanding:
            raise StaleBaseline(
                f"the post-seed dump does not match fixtures.json for {customer.code}; "
                f"delete {dolictl.DUMP_FILE} and run the live tests again to re-seed"
            )
    return fixtures
