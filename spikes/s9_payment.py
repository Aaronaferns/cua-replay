"""Spike S9: can a payment be committed through the Dolibarr UI, and does the lost-504 fault leave exactly one?

Drives the payment form with Playwright inside a `new_guarded_context` (through `cua.control.runtime.open_session`, with a
policy copy that allows commits). The oracle is Dolibarr's own REST `GET /invoices/{id}/payments` (count and amounts),
never the UI. Three probes, each on a fresh seeded invoice state:

  a  record a payment on a validated, unpaid seeded invoice
  b  an overpayment shows Dolibarr's own error and creates no payment
  c  lost 504: the confirm request is really sent (`route.fetch()`), the browser is told 504 (`route.fulfill`);
     exactly one payment must exist afterwards

It also records the commit route: the URL, method and body of the payment confirm (what M6's `commit_routes` must
describe). The target is reset to the post-seed baseline afterwards.

    uv run python spikes/s9_payment.py
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "target" / "dolibarr"))

import dolictl  # noqa: E402
import seed  # noqa: E402
from dolirest import Rest, customer_by_code, invoices_of  # noqa: E402
from livetarget import reset_to_baseline  # noqa: E402
from playwright.async_api import Page, Route  # noqa: E402

from cua.config import settings  # noqa: E402
from cua.control.runtime import open_session  # noqa: E402
from cua.safety.policy import Policy  # noqa: E402

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


def commit_policy(tmp: Path) -> str:
    raw = json.loads((ROOT / "policies" / "dolibarr-sandbox.json").read_text(encoding="utf-8"))
    raw["name"] = "dolibarr-sandbox-s9"
    raw["max_effect"] = "commit"  # the shipped sandbox policy stops at "read"; the spike needs the payment route
    path = tmp / "s9-policy.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return str(path)


async def enter_payment(page: Page, base: str, invoice_id: str, amount: str) -> None:
    await page.goto(f"{base}{PAY_PATH}?facid={invoice_id}&action=create")
    await page.select_option("select[name=paiementcode]", "LIQ")
    await page.select_option("select[name=accountid]", label="CUA Seed Operating Account (EUR)")
    await page.get_by_role("button", name="Now").click()
    await page.fill(f"input[name=amount_{invoice_id}]", amount)
    await page.click("input[type=submit][value=Pay]")  # POST action=add_paiement: asks for confirmation, creates nothing
    await page.wait_for_load_state("load")


async def page_text(page: Page) -> str:
    return re.sub(r"\s+", " ", await page.evaluate("document.body.innerText"))


async def main() -> int:
    cfg = settings()
    base = cfg.browser_base_url
    reset()
    fixtures = seed.load_fixtures()
    admin = Rest.admin(cfg)
    alpha = customer_by_code(admin, fixtures.customer("alpha").code)
    beta = customer_by_code(admin, fixtures.customer("beta").code)
    inv_a = str(invoices_of(admin, alpha["id"])[0]["id"])  # 120.40 remaining
    inv_c = str(invoices_of(admin, beta["id"])[0]["id"])  # 310.00 remaining
    assert payments(admin, inv_a) == [] and payments(admin, inv_c) == []

    rows: list[tuple[str, str, str]] = []
    commit_requests: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        async with open_session(run_id="spike-s9", policy=commit_policy(Path(tmp)), evidence_root=Path(tmp)) as s:
            page, policy = s.surface.page, s.policy
            assert isinstance(policy, Policy) and policy.max_effect == "commit"

            def log(request) -> None:
                if request.method != "OPTIONS" and urlsplit(request.url).path == PAY_PATH and request.method == "POST":
                    body = dict(parse_qsl(request.post_data or "", keep_blank_values=True))
                    commit_requests.append({"method": request.method, "path": urlsplit(request.url).path,
                                            "query": urlsplit(request.url).query, "action": body.get("action"),
                                            "confirm": body.get("confirm")})

            page.on("request", log)

            # ---- a: record a payment -----------------------------------------------------------------------
            await enter_payment(page, base, inv_a, "50.00")
            text = await page_text(page)
            asked = "Do you confirm this payment input for 50 Euros?" in text
            before_confirm = payments(admin, inv_a)
            await page.click("input.confirmvalidatebutton")  # confirm=yes (default) + Validate: the commit
            await page.wait_for_load_state("load")
            after = payments(admin, inv_a)
            ok_a = asked and before_confirm == [] and after == [Decimal("50.00")]
            rows.append(("a record payment", "PASS" if ok_a else "FAIL",
                         f"confirm asked={asked}; payments before confirm={before_confirm}; after={[str(x) for x in after]}"))

            # ---- b: overpayment ----------------------------------------------------------------------------
            # Measured: Dolibarr does not refuse at the form. It shows its own warning ("the payment amount ... is
            # higher than the outstanding amount to pay") and preselects "No" in the confirmation; only an explicit
            # "Yes" would create the overpayment (probe b'' below). So "no payment" holds for the default path.
            await enter_payment(page, base, inv_a, "500.00")
            text = await page_text(page)
            warning = "higher than the outstanding amount" in text
            preselected = await page.input_value("select#confirm")
            await page.click("input.confirmvalidatebutton")  # the default answer
            await page.wait_for_load_state("load")
            after_b = payments(admin, inv_a)
            ok_b = warning and preselected == "no" and after_b == [Decimal("50.00")]
            rows.append(("b overpayment (default answer)", "PASS" if ok_b else "FAIL",
                         f"dolibarr warning shown={warning}; preselected answer={preselected!r}; "
                         f"payments={[str(x) for x in after_b]}"))
            asked_b = True

            if asked_b:  # what if a person (or a script) explicitly answers Yes to Dolibarr's warning?
                await enter_payment(page, base, inv_a, "500.00")
                await page.select_option("select#confirm", "yes")
                await page.click("input.confirmvalidatebutton")
                await page.wait_for_load_state("load")
                forced = payments(admin, inv_a)
                rows.append(("b'' overpayment, explicit Yes", "INFO",
                             f"payments={[str(x) for x in forced]} (Dolibarr {'accepted' if len(forced) > 1 else 'refused'} it)"))

            # ---- c: lost 504 on the confirm ----------------------------------------------------------------
            sent: list[int] = []

            async def lose_the_response(route: Route) -> None:
                req = route.request
                body = dict(parse_qsl(req.post_data or "", keep_blank_values=True))
                is_confirm = (req.method == "POST" and urlsplit(req.url).path == PAY_PATH
                              and body.get("action") == "confirm_paiement" and body.get("confirm") == "yes")
                if not is_confirm or not policy.check_url(req.url).allowed:
                    await route.fallback()  # everything else continues to the guard's own route
                    return
                response = await route.fetch(max_redirects=0)  # the real request reaches the server
                sent.append(response.status)
                await route.fulfill(status=504, body="Gateway Time-out")  # ...and the browser never sees the answer

            statuses: list[int] = []
            page.on("response", lambda r: statuses.append(r.status)
                    if r.request.method == "POST" and urlsplit(r.url).path == PAY_PATH else None)
            await page.context.route("**/*", lose_the_response)  # added after the guard's route, so it runs first
            await enter_payment(page, base, inv_c, "100.00")
            await page.click("input.confirmvalidatebutton")
            await page.wait_for_load_state("load")
            shown = await page_text(page)
            await page.context.unroute("**/*", lose_the_response)
            after_c = payments(admin, inv_c)
            ok_c = after_c == [Decimal("100.00")] and bool(sent) and 504 in statuses
            rows.append(("c lost 504 on confirm", "PASS" if ok_c else "FAIL",
                         f"server answered {sent} to the real request; browser got {statuses}, page shows {shown[:40]!r}; "
                         f"payments={[str(x) for x in after_c]}"))

    reset()
    restored = payments(admin, inv_a) == [] and payments(admin, inv_c) == []
    rows.append(("reset to baseline", "PASS" if restored else "FAIL", "both invoices have no payments again"))

    width = max(len(r[0]) for r in rows)
    for name, verdict, detail in rows:
        print(f"{name:<{width}}  {verdict}  {detail}")
    print("\ncommit requests seen (POST to the payment page):")
    for r in commit_requests:
        print("  ", r)
    verdict = all(r[1] in ("PASS", "INFO") for r in rows)
    print("\nS9:", "PASS" if verdict else "FAIL (fallback: invoice validation is the commit capability)")
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
