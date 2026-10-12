"""A small Dolibarr REST client plus the ground-truth helpers the fixtures, tests and evidence use.

The REST API is a side channel: fixtures (`seed.py`), tests and evidence ground truth use it. Discovery and replay
never do; they act through the UI. Keys and passwords are `SecretStr` and are unwrapped only when a request is built;
no error message or return value carries them.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from cua.config import Settings

HERE = Path(__file__).resolve().parent
FIXTURES_FILE = HERE / "fixtures.json"
DEMO_KEY = SecretStr("demokey")  # the demo data's admin key; the seed replaces it with DOLIBARR_API_KEY


class RestError(Exception):
    """A REST call failed. Carries the method, path and status; never a key, a password or a response body."""

    def __init__(self, method: str, path: str, status: int):
        super().__init__(f"{method} {path} -> HTTP {status}")
        self.status = status


@dataclass(frozen=True)
class Customer:
    key: str
    code: str
    name: str
    invoices: tuple[tuple[str, Decimal], ...]  # (ref_client, amount incl. tax)

    @property
    def outstanding(self) -> Decimal:
        """What the seed leaves unpaid and validated for this customer (the card's "Current outstanding bill")."""
        return sum((amount for _, amount in self.invoices), Decimal("0.00"))


@dataclass(frozen=True)
class Fixtures:
    customers: tuple[Customer, ...]
    near_miss_existing_code: str  # a demo customer whose code extends `near_miss_missing_code`
    near_miss_missing_code: str  # exists nowhere; a LIKE search for it finds only `near_miss_existing_code`
    bank_ref: str
    bank_label: str
    readonly_login: str
    noperm_login: str

    def customer(self, key: str) -> Customer:
        return next(c for c in self.customers if c.key == key)


def load_fixtures(path: Path = FIXTURES_FILE) -> Fixtures:
    raw = json.loads(path.read_text(encoding="utf-8"))
    customers = tuple(
        Customer(
            key=c["key"], code=c["code"], name=c["name"],
            invoices=tuple((i["ref_client"], Decimal(i["amount"])) for i in c["invoices"]),
        )
        for c in raw["customers"]
    )
    return Fixtures(
        customers=customers,
        near_miss_existing_code=raw["near_miss"]["existing_code"],
        near_miss_missing_code=raw["near_miss"]["missing_code"],
        bank_ref=raw["bank_account"]["ref"],
        bank_label=raw["bank_account"]["label"],
        readonly_login=raw["users"]["readonly_login"],
        noperm_login=raw["users"]["noperm_login"],
    )


def derived_password(settings: Settings, purpose: str) -> SecretStr:
    """A password for a fixture account that has no setting of its own, derived from the run password so that the
    seed and the tests agree without another secret in .env. Local throwaway accounts only."""
    base = settings.cua_run_password.get_secret_value() if settings.cua_run_password else ""
    return SecretStr(hashlib.sha256(f"{base}:{purpose}".encode()).hexdigest()[:24])


class Rest:
    def __init__(self, base_url: str, key: SecretStr | None, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.key = key
        self.timeout = timeout

    @classmethod
    def admin(cls, settings: Settings) -> Rest:
        return cls(settings.api_base_url, settings.dolibarr_api_key)

    def with_key(self, key: SecretStr | None) -> Rest:
        return Rest(self.base_url, key, self.timeout)

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        """(status, parsed JSON or None). HTTP error statuses are returned, not raised; use `ok` helpers for that."""
        headers = {"Accept": "application/json"}
        if self.key is not None:
            headers["DOLAPIKEY"] = self.key.get_secret_value()
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            return e.code, None

    def ok(self, method: str, path: str, body: Any = None) -> Any:
        status, data = self.call(method, path, body)
        if status != 200:
            raise RestError(method, path, status)
        return data

    def get(self, path: str, **params: Any) -> Any:
        return self.ok("GET", path + _query(params))

    def listing(self, path: str, **params: Any) -> list[dict[str, Any]]:
        """A list endpoint. Dolibarr answers 404 for an empty result; that is an empty list here."""
        status, data = self.call("GET", path + _query({"limit": 1000, **params}))
        if status == 404:
            return []
        if status != 200:
            raise RestError("GET", path, status)
        return data


def _query(params: dict[str, Any]) -> str:
    if not params:
        return ""
    return "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}, quote_via=urllib.parse.quote)


def sql_eq(column: str, value: str) -> str:
    """A `sqlfilters` equality on a string column. Values are fixture identities, so quotes are refused outright."""
    if "'" in value or ")" in value or "(" in value:
        raise ValueError("fixture values must not contain quotes or parentheses")
    return f"({column}:=:'{value}')"


# -- ground truth -------------------------------------------------------------------------------------------------


def customer_by_code(rest: Rest, code: str) -> dict[str, Any] | None:
    found = rest.listing("/thirdparties", sqlfilters=sql_eq("t.code_client", code))
    return found[0] if found else None


def invoices_of(rest: Rest, socid: int | str) -> list[dict[str, Any]]:
    return rest.listing("/invoices", thirdparty_ids=str(socid), sortfield="t.rowid")


def remaining(rest: Rest, invoice: dict[str, Any]) -> Decimal:
    """total incl. tax minus what was paid, from the payments endpoint (the posting oracle), not from a UI."""
    paid = Decimal("0")
    status, payments = rest.call("GET", f"/invoices/{invoice['id']}/payments")
    if status == 200 and payments:
        paid = sum((Decimal(str(p["amount"])) for p in payments), Decimal("0"))
    return (Decimal(str(invoice["total_ttc"])) - paid).quantize(Decimal("0.01"))


def outstanding_of(rest: Rest, socid: int | str) -> Decimal:
    """The sum a customer still owes on validated, unpaid invoices: what Dolibarr's card calls "Current outstanding
    bill". Drafts and abandoned invoices do not count."""
    total = Decimal("0.00")
    for invoice in invoices_of(rest, socid):
        if str(invoice.get("statut", invoice.get("status"))) == "1" and str(invoice.get("paye", "0")) == "0":
            total += remaining(rest, invoice)
    return total


_AMOUNT = re.compile(r"-?\d[\d,.\s  ]*")


def parse_amount(text: str) -> Decimal:
    """The last money amount in text such as 'Current outstanding bill 1,075.37 €' (also '1 075,37 €')."""
    matches = [m.group(0).strip() for m in _AMOUNT.finditer(text)]
    if not matches:
        raise ValueError("no amount in text")
    raw = re.sub(r"[\s  ]", "", matches[-1])
    if "," in raw and "." in raw:
        raw = raw.replace(",", "") if raw.rfind(".") > raw.rfind(",") else raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        head, _, tail = raw.rpartition(",")
        raw = f"{head}.{tail}" if len(tail) == 2 else raw.replace(",", "")
    return Decimal(raw).quantize(Decimal("0.01"))


def state_snapshot(rest: Rest) -> dict[str, Any]:
    """The REST view of everything the fixtures and flows touch, for before/after comparison. Volatile per-login
    fields are left out."""
    thirdparties = rest.listing("/thirdparties", sortfield="t.rowid")
    invoices = rest.listing("/invoices", sortfield="t.rowid")
    return {
        "thirdparties": [(t["id"], t["name"], t.get("code_client"), str(t.get("status", t.get("statut")))) for t in thirdparties],
        "invoices": [
            (i["id"], i["ref"], i.get("ref_client"), i["socid"], str(i.get("statut", i.get("status"))),
             str(i.get("paye")), str(i["total_ttc"]), str(i.get("totalpaid")))
            for i in invoices
        ],
        "bankaccounts": [(b["id"], b["ref"], b["label"]) for b in rest.listing("/bankaccounts")],
        "users": [(u["id"], u["login"], str(u.get("statut", u.get("status")))) for u in rest.listing("/users")],
    }
