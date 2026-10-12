"""Spike S10 as a test: the post-seed dump restores the known state, fast, and the REST view is identical."""

from __future__ import annotations

import re
import urllib.request

import dolictl
import pytest
from dolirest import Rest, state_snapshot
from livetarget import reset_to_baseline

from cua.config import settings

pytestmark = pytest.mark.live


def _login_page() -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{settings().dolibarr_port}/index.php", timeout=10) as r:
        return r.read().decode("utf-8", "replace")


def test_reset_restores_known_state():
    reset_to_baseline()
    admin = Rest.admin(settings())
    before = state_snapshot(admin)

    # change what flows change: a new customer, a new invoice, and a console setting (the login CAPTCHA)
    socid = admin.ok("POST", "/thirdparties", {"name": "Reset Probe", "code_client": "CU2601-09999", "client": 1})
    admin.ok("POST", "/invoices", {"socid": socid, "type": 0, "ref_client": "RESET-PROBE",
                                   "lines": [{"desc": "x", "qty": 1, "subprice": 1.0, "tva_tx": 0, "product_type": 1}]})
    dolictl.run_script("captcha.sh", "on")
    assert 'name="code"' in _login_page()
    assert state_snapshot(admin) != before

    out, _ = dolictl.run_script("reset.sh")
    seconds = float(re.search(r"in ([0-9.]+)s", out).group(1))
    print(f"S10: {out.strip()}")
    assert seconds < 15, out
    assert state_snapshot(admin) == before
    assert 'name="code"' not in _login_page()
