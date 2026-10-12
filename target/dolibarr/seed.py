"""Create the target's known fixtures through the REST API. Idempotent: run it as often as you like.

    uv run python target/dolibarr/seed.py [--dump]

What it makes (identities in fixtures.json, secrets from the repo .env):
  - the admin REST key from DOLIBARR_API_KEY replaces the demo data's `demokey` (which must not stay valid);
  - customers with known codes, and validated, unpaid, untaxed invoices with known amounts;
  - the run user (CUA_RUN_LOGIN / CUA_RUN_PASSWORD), a read-only user, and the demo user `noperm` with a known password;
  - a bank account.
It then checks, through the REST login of each account, that the rights are what the fixtures promise.

Dolibarr's REST API can create users and set passwords but cannot grant rights, so `grant_rights` inserts the
`llx_user_rights` rows directly through the database container: the only non-REST step, and fixture-only.
`--dump` finishes by saving the post-seed dump that `reset.sh` restores.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pydantic import SecretStr  # noqa: E402

import dolictl as ctl  # noqa: E402
from dolirest import (  # noqa: E402
    DEMO_KEY,
    Customer,
    Fixtures,
    Rest,
    RestError,
    customer_by_code,
    derived_password,
    invoices_of,
    load_fixtures,
    outstanding_of,
    sql_eq,
)

from cua.config import Settings, settings  # noqa: E402

# (module, perms, subperms or None): the rows of Dolibarr's rights table each account gets.
RUN_RIGHTS = [
    ("societe", "lire", None), ("societe", "creer", None), ("societe", "client", "voir"),
    ("facture", "lire", None), ("facture", "creer", None), ("facture", "invoice_advance", "validate"),
    ("facture", "paiement", None),
    ("banque", "lire", None), ("banque", "modifier", None),
]
READONLY_RIGHTS = [
    ("societe", "lire", None), ("societe", "client", "voir"),
    ("facture", "lire", None),
    ("banque", "lire", None),
]


class SeedError(Exception):
    """A fixture could not be created or did not check out. Messages never contain keys or passwords."""


def say(message: str) -> None:
    print(message, flush=True)


# -- the admin key ------------------------------------------------------------------------------------------------


def ensure_admin_key(cfg: Settings) -> Rest:
    """Make DOLIBARR_API_KEY the admin's working key and `demokey` invalid; returns the REST client using it."""
    wanted = cfg.dolibarr_api_key
    if wanted is None:
        raise SeedError("DOLIBARR_API_KEY is not set in .env")
    if not re.fullmatch(r"[A-Za-z0-9_\-]{8,}", wanted.get_secret_value()):
        raise SeedError("DOLIBARR_API_KEY must be at least 8 characters of letters, digits, '_' or '-'")
    admin = Rest.admin(cfg)
    demo = admin.with_key(DEMO_KEY)
    wanted_works = admin.call("GET", "/users?limit=1")[0] == 200
    demo_works = demo.call("GET", "/users?limit=1")[0] == 200
    if not wanted_works:
        if not demo_works:
            raise SeedError("neither DOLIBARR_API_KEY nor the demo key is accepted: is the target up and fresh?")
        row = demo.listing("/users", sqlfilters=sql_eq("t.login", "admin"))
        if not row:
            raise SeedError("the demo data has no 'admin' user")
        status, _ = demo.call("PUT", f"/users/{row[0]['id']}", {"api_key": wanted.get_secret_value()})
        if status != 200 or admin.call("GET", "/users?limit=1")[0] != 200:
            say("REST did not take the new admin key; setting it in the database instead")
            ctl.sql(cfg, f"UPDATE llx_user SET api_key = '{wanted.get_secret_value()}' WHERE login = 'admin';")
        if admin.call("GET", "/users?limit=1")[0] != 200:
            raise SeedError("DOLIBARR_API_KEY is still not accepted after setting it")
        say("admin API key set from .env")
    if wanted.get_secret_value() != DEMO_KEY.get_secret_value() and demo.call("GET", "/users?limit=1")[0] == 200:
        raise SeedError("the demo key 'demokey' is still accepted")
    return admin


# -- customers and invoices ---------------------------------------------------------------------------------------


def ensure_customer(admin: Rest, customer: Customer) -> int:
    found = customer_by_code(admin, customer.code)
    if found:
        return int(found["id"])
    socid = admin.ok("POST", "/thirdparties", {"name": customer.name, "code_client": customer.code, "client": 1})
    say(f"created customer {customer.code}")
    return int(socid)


def ensure_invoices(admin: Rest, customer: Customer, socid: int) -> None:
    existing = {i.get("ref_client"): i for i in invoices_of(admin, socid)}
    for ref_client, amount in customer.invoices:
        invoice = existing.get(ref_client)
        if invoice is None:
            invoice_id = admin.ok("POST", "/invoices", {
                "socid": socid, "type": 0, "ref_client": ref_client,
                "lines": [{"desc": "Seeded service", "qty": 1, "subprice": float(amount), "tva_tx": 0,
                           "product_type": 1}],
            })
            say(f"created invoice {ref_client}")
        else:
            invoice_id = int(invoice["id"])
        if invoice is None or str(invoice.get("statut", invoice.get("status"))) == "0":
            admin.ok("POST", f"/invoices/{invoice_id}/validate", {"idwarehouse": 0, "notrigger": 0})
            say(f"validated invoice {ref_client}")


# -- users --------------------------------------------------------------------------------------------------------


def ensure_user(admin: Rest, login: str, password: SecretStr, *, firstname: str, lastname: str) -> int:
    found = admin.listing("/users", sqlfilters=sql_eq("t.login", login))
    if found:
        user_id = int(found[0]["id"])
    else:
        user_id = int(admin.ok("POST", "/users", {"login": login, "firstname": firstname, "lastname": lastname}))
        say(f"created user {login}")
    admin.ok("PUT", f"/users/{user_id}", {"pass": password.get_secret_value()})
    return user_id


def grant_rights(cfg: Settings, user_id: int, rights: list[tuple[str, str, str | None]]) -> None:
    wanted = " OR ".join(
        f"(d.module = '{m}' AND d.perms = '{p}' AND "
        + ("(d.subperms IS NULL OR d.subperms = '')" if s is None else f"d.subperms = '{s}'")
        + ")"
        for m, p, s in rights
    )
    ctl.sql(cfg, f"INSERT IGNORE INTO llx_user_rights (entity, fk_user, fk_id) "
                 f"SELECT 1, {int(user_id)}, d.id FROM llx_rights_def d WHERE d.entity = 1 AND ({wanted});")
    have = {
        tuple(line.split("\t"))
        for line in ctl.sql(cfg, f"SELECT d.module, d.perms, COALESCE(d.subperms, '') FROM llx_user_rights r "
                                 f"JOIN llx_rights_def d ON d.id = r.fk_id WHERE r.fk_user = {int(user_id)};").splitlines()
    }
    missing = [r for r in rights if (r[0], r[1], r[2] or "") not in have]
    if missing:
        raise SeedError(f"rights not found in this Dolibarr's rights table: {missing}")


def user_key(cfg: Settings, login: str) -> SecretStr:
    return derived_password(cfg, f"apikey:{login}")


def set_user_key(cfg: Settings, user_id: int, login: str) -> None:
    """Give a fixture user a REST key. Dolibarr's REST login refuses users that have none and may not mint their own,
    and REST does not store `api_key` from PUT, so the key goes straight into the database (plain, as the demo's)."""
    key = user_key(cfg, login).get_secret_value()
    ctl.sql(cfg, f"UPDATE llx_user SET api_key = '{key}' WHERE rowid = {int(user_id)};")


def as_user(cfg: Settings, login: str) -> Rest:
    """An API client acting as that user."""
    return Rest(cfg.api_base_url, user_key(cfg, login))


def verify_access(cfg: Settings, fixtures: Fixtures) -> None:
    """The accounts do what the fixtures promise: the run user reads and writes, the read-only user only reads, the
    no-rights user is refused."""
    run = as_user(cfg, cfg.cua_run_login)
    readonly = as_user(cfg, fixtures.readonly_login)
    noperm = as_user(cfg, fixtures.noperm_login)
    expect = [
        ("run user reads customers", run, "GET", "/thirdparties?limit=1", None, 200),
        ("run user reads invoices", run, "GET", "/invoices?limit=1", None, 200),
        ("read-only user reads customers", readonly, "GET", "/thirdparties?limit=1", None, 200),
        ("read-only user reads invoices", readonly, "GET", "/invoices?limit=1", None, 200),
        ("read-only user cannot create invoices", readonly, "POST", "/invoices", {}, 403),
        ("no-rights user cannot read customers", noperm, "GET", "/thirdparties?limit=1", None, 403),
        ("no-rights user cannot read invoices", noperm, "GET", "/invoices?limit=1", None, 403),
    ]
    failures = []
    for what, client, method, path, body, status in expect:
        got = client.call(method, path, body)[0]
        if got != status:
            failures.append(f"{what}: expected HTTP {status}, got {got}")
    if failures:
        raise SeedError("access check failed:\n  " + "\n  ".join(failures))


# -- bank account -------------------------------------------------------------------------------------------------


def ensure_bank_account(admin: Rest, fixtures: Fixtures) -> None:
    accounts = admin.listing("/bankaccounts")
    if any(a.get("ref") == fixtures.bank_ref for a in accounts):
        return
    template = accounts[0] if accounts else {}
    admin.ok("POST", "/bankaccounts", {
        "ref": fixtures.bank_ref, "label": fixtures.bank_label, "courant": 1, "type": 1,
        "currency_code": template.get("currency_code") or "EUR",
        "country_id": int(template.get("country_id") or 1),
    })
    say(f"created bank account {fixtures.bank_ref}")


# -- main ---------------------------------------------------------------------------------------------------------


def seed(cfg: Settings | None = None) -> Fixtures:
    cfg = cfg or settings()
    fixtures = load_fixtures()
    for name, value in (("CUA_RUN_PASSWORD", cfg.cua_run_password), ("CUA_NOPERM_PASSWORD", cfg.cua_noperm_password)):
        if value is None:
            raise SeedError(f"{name} is not set in .env")
    admin = ensure_admin_key(cfg)

    for customer in fixtures.customers:
        ensure_invoices(admin, customer, ensure_customer(admin, customer))
    ensure_bank_account(admin, fixtures)

    run_id = ensure_user(admin, cfg.cua_run_login, cfg.cua_run_password, firstname="Run", lastname="Operator")
    ro_id = ensure_user(admin, fixtures.readonly_login, derived_password(cfg, "readonly"),
                        firstname="Read", lastname="Only")
    noperm = admin.listing("/users", sqlfilters=sql_eq("t.login", fixtures.noperm_login))
    if not noperm:
        raise SeedError("the demo data has no 'noperm' user")
    admin.ok("PUT", f"/users/{noperm[0]['id']}", {"pass": cfg.cua_noperm_password.get_secret_value()})
    set_user_key(cfg, run_id, cfg.cua_run_login)
    set_user_key(cfg, ro_id, fixtures.readonly_login)
    set_user_key(cfg, int(noperm[0]["id"]), fixtures.noperm_login)
    grant_rights(cfg, run_id, RUN_RIGHTS)
    grant_rights(cfg, ro_id, READONLY_RIGHTS)
    verify_access(cfg, fixtures)

    for customer in fixtures.customers:
        socid = int(customer_by_code(admin, customer.code)["id"])
        got = outstanding_of(admin, socid)
        if got != customer.outstanding:
            raise SeedError(f"{customer.code}: outstanding is {got}, the fixture promises {customer.outstanding}")
    return fixtures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dump", action="store_true", help="save the post-seed dump afterwards")
    args = parser.parse_args(argv)
    try:
        fixtures = seed()
        if args.dump:
            say(ctl.run_script("reset.sh", "dump")[0].strip())
    except (SeedError, RestError, ctl.CtlError) as e:
        say(f"seed failed: {e}")
        return 1
    say("fixtures:")
    for c in fixtures.customers:
        say(f"  {c.code}  {c.name:<24} outstanding {c.outstanding:>8}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
