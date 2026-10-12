"""M6 fault on the real Dolibarr target: the payment confirm is really sent, the browser is told 504, and exactly one
payment exists afterwards. The oracle is Dolibarr's own REST `GET /invoices/{id}/payments`, never the UI.

Needs a policy whose ceiling is `commit`: `policies/dolibarr-sandbox-commit.json` (the default sandbox policy stays at
`read`). The test drives the page directly (it tests the fault, not the surface) inside the guarded context that
`open_session` builds. The target is reset to the baseline before and after.
"""

from __future__ import annotations

import contextlib
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "target" / "dolibarr"))

import dolictl  # noqa: E402
from dolirest import Rest, customer_by_code, invoices_of  # noqa: E402
from livetarget import reset_to_baseline  # noqa: E402
from seed import load_fixtures  # noqa: E402

from cua.config import settings  # noqa: E402
from cua.control.runtime import open_session  # noqa: E402
from cua.faults.harness import FaultEvent, LostResponseFault, install_faults  # noqa: E402

pytestmark = pytest.mark.live

PAY_PATH = "/compta/paiement.php"


def reset() -> None:
    try:
        reset_to_baseline()
    except dolictl.CtlError as e:
        if "budget" not in str(e):  # the restore happened; only its 15 s budget was exceeded on a loaded host
            raise


def payments(rest: Rest, invoice_id: str) -> list[Decimal]:
    status, body = rest.call("GET", f"/invoices/{invoice_id}/payments")
    if status == 404:
        return []  # Dolibarr answers 404 "No payment" for an invoice without payments
    assert status == 200, status
    return sorted(Decimal(str(p["amount"])) for p in body)


async def test_lost_response_after_commit_leaves_exactly_one_payment(tmp_path):
    reset()
    try:
        admin = Rest.admin(settings())
        beta = customer_by_code(admin, load_fixtures().customer("beta").code)
        invoice = str(invoices_of(admin, beta["id"])[0]["id"])  # 310.00 remaining
        assert payments(admin, invoice) == []

        fired: list[FaultEvent] = []
        async with open_session(
            run_id="fault-live", policy="dolibarr-sandbox-commit", evidence_root=tmp_path
        ) as session:
            page, base = session.surface.page, session.base_url
            assert session.policy.max_effect == "commit" and session.policy.sandbox
            statuses: list[int] = []
            page.on("response", lambda r: statuses.append(r.status) if r.request.method == "POST" else None)

            install = await install_faults(
                page.context,
                session.policy,
                [LostResponseFault(match=PAY_PATH, body_fields={"action": "confirm_paiement", "confirm": "yes"})],
                on_fault=fired.append,
            )
            await page.goto(f"{base}{PAY_PATH}?facid={invoice}&action=create")
            await page.select_option("select[name=paiementcode]", "LIQ")
            await page.select_option("select[name=accountid]", label="CUA Seed Operating Account (EUR)")
            await page.get_by_role("button", name="Now").click()
            await page.fill(f"input[name=amount_{invoice}]", "100.00")
            await page.click("input[type=submit][value=Pay]")  # asks for confirmation; creates nothing
            await page.wait_for_load_state("load")
            assert payments(admin, invoice) == []

            await page.click("input.confirmvalidatebutton")  # the commit: confirm_paiement, confirm=yes
            await page.wait_for_load_state("load")

            assert install.count(0) == 1
            assert fired[0].kind == "lost_response_after_commit" and fired[0].detail["status"] == 504
            assert fired[0].detail["server_status"] < 400  # the server really processed it
            assert statuses[-1] == 504  # the browser was told the request failed
            assert payments(admin, invoice) == [Decimal("100.00")]  # ...yet exactly one payment exists
            await install.uninstall()
    finally:
        with contextlib.suppress(Exception):
            reset()
