"""A payment form on top of the in-memory fake, with a server-side payment list as the oracle.

`mode` decides what Validate does: "ok" records the payment and shows it; "overpay" records nothing and warns; "lost"
records it and shows a gateway timeout (the response was lost); "expire" records it and drops the session.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from fakes import BASE, FakeDolibarr, Page
from sample_docs import dolibarr_profile

from cua.artifact.schema import Capability, Profile


class PayWorld(FakeDolibarr):
    def __init__(self, guard, mode: str = "ok") -> None:
        super().__init__(guard)
        self.mode, self.stage = mode, "form"
        self.payments: list[str] = []
        self.clicks = 0
        self.tamper: str | None = None  # the amount the page shows instead of the one requested

    def _render(self) -> Page:
        parts = urlsplit(self.url)
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        if not self.logged_in or parts.path not in ("/compta/paiement.php", "/compta/facture/card.php"):
            return super()._render()
        e = self._el
        if parts.path == "/compta/facture/card.php":
            return Page(self.url, "Invoice", [
                e(role="link", name="Customer invoice", tag="a"), e(role="cell", name="Payment recorded"),
                e(role="cell", text="PAY-0001", tag="td", row_label="Payment", take="value"),
                e(role="cell", text=q["amount"], tag="td", row_label="Amount paid", take="value")])
        if self.stage == "timeout":
            return Page(self.url, "504", [e(role="heading", name="Gateway Timeout")])

        def pay() -> None:
            self.clicks += 1
            if self.mode == "overpay":
                self.stage = "overpay"
                return
            self.payments.append(q["amount"])
            if self.mode == "lost":
                self.stage = "timeout"
            elif self.mode == "expire":
                self.logged_in = False
            else:
                self._goto(f"{BASE}/compta/facture/card.php?facid=1&amount={q['amount']}")

        shown = {"Invoice": q["facid"], "Amount": self.tamper or q["amount"], "Bank account": q["bank"]}
        elements = [e(role="cell", name="Enter payment received from customer")]
        elements += [e(role="cell", text=v, tag="td", row_label=k, take="value") for k, v in shown.items()]
        if self.stage == "overpay":
            elements.append(e(role="cell", name="Payment exceeds the remaining amount"))
        elements.append(e(role="textbox", name="Note", tag="input", input_type="text", css="input[name='note']"))
        elements.append(e(role="button", name="Validate", tag="input", input_type="submit", on_click=pay,
                          css="input[type=submit]"))
        return Page(self.url, "Payment", elements)


def profile() -> Profile:
    doc = dolibarr_profile().model_dump(mode="json", by_alias=True, exclude_none=True)
    doc["states"]["payment_recorded"] = {
        "description": "Invoice card showing the recorded payment.", "specializes": "invoice_card",
        "origin": "declared", "signature": {"kind": "all", "of": [
            {"kind": "url_matches", "pattern": "/compta/facture/card.php*"},
            {"kind": "text", "equals": "Customer invoice", "role": "link"},
            {"kind": "text", "equals": "Payment recorded", "role": "cell"}]}}
    doc["states"]["overpayment"] = {
        "description": "Payment form warning that the amount exceeds what is due.", "specializes": "payment_form",
        "origin": "declared", "signature": {"kind": "all", "of": [
            {"kind": "url_matches", "pattern": "/compta/paiement.php*"},
            {"kind": "text", "equals": "Enter payment received from customer", "role": "cell"},
            {"kind": "text", "equals": "Payment exceeds the remaining amount", "role": "cell"}]}}
    return Profile.model_validate(doc)


def _cell(label: str) -> dict:
    return {"description": label, "strategies": [{"kind": "same_row", "label": label, "take": "value",
                                                  "why": "value beside the label", "matched_at_record": "1 of 1"}],
            "fingerprint": {"role": "cell", "tag": "td", "row_label": label}}


def _same(target: str, name: str) -> dict:
    return {"kind": "target_equals_input", "target": target, "input": name, "normalizer": "text"}


def _review(sid: str, target: str, name: str) -> dict:
    return {"id": sid, "intent": f"The form shows the requested {name}", "expect": "payment_form", "effect": "none",
            "do": {"kind": "assert", "that": _same(target, name), "on_fail": "IDENTITY_MISMATCH"}}


def capability_dict() -> dict:
    return {
        "schema": "cua.capability/v1", "id": "dolibarr.record_invoice_payment", "version": "1.0.0",
        "title": "Record a payment", "description": "Record a payment against an invoice.", "app": "dolibarr",
        "contract": {
            "inputs": {n: {"type": t} for n, t in (("invoice_ref", "string"), ("amount", "decimal"),
                                                   ("bank_account", "string"), ("request_id", "string"))},
            "outputs": {"payment_ref": {"type": "string"}},
            "outcomes": {"validation_rejected": {"description": "the application refused the payment"}}},
        "effect": "commit",
        "steps": [
            {"id": "s01_open_form", "intent": "Open the payment form", "expect": "home", "effect": "read",
             "navigates": True, "do": {"kind": "navigate",
                                       "route": "/compta/paiement.php?facid=${invoice_ref}&amount=${amount}&bank=${bank_account}"}},
            _review("s02_review_invoice", "invoice_shown", "invoice_ref"),
            _review("s03_review_amount", "amount_shown", "amount"),
            _review("s04_review_bank", "bank_shown", "bank_account"),
            {"id": "s05_commit", "intent": "Validate the payment", "expect": "payment_form", "effect": "commit",
             "navigates": True, "do": {"kind": "click", "target": "validate"},
             "on": [{"state": "overpayment", "outcome": "validation_rejected"}],
             "verify": {"kind": "all", "of": [_same("paid_amount", "amount"),
                                              {"kind": "target_visible", "target": "payment_ref"}]}},
            {"id": "s06_read_ref", "intent": "Read the payment reference", "expect": "payment_recorded",
             "effect": "read", "do": {"kind": "extract", "target": "payment_ref", "output": "payment_ref"}},
        ],
        "success": {"state": "payment_recorded", "outputs": ["payment_ref"], "checks": []},
        "targets": {
            "invoice_shown": _cell("Invoice"), "amount_shown": _cell("Amount"), "bank_shown": _cell("Bank account"),
            "paid_amount": _cell("Amount paid"), "payment_ref": _cell("Payment"),
            "validate": {"description": "the Validate button", "fingerprint": {
                "role": "button", "tag": "input", "input_type": "submit"},
                         "strategies": [{"kind": "role", "role": "button", "name": "Validate", "why": "the submit",
                                         "matched_at_record": "1 of 1"}]}},
        "provenance": {"trace_run_id": "run-test", "trace_sha256": "0" * 64, "discovered_by": "alice",
                       "compiler": "cua.compiler/v0"},
    }


def capability(**changes) -> Capability:
    return Capability.model_validate({**capability_dict(), **changes})


INPUTS = {"invoice_ref": "INV-1", "amount": "50.00", "bank_account": "BANK1", "request_id": "req-1"}
