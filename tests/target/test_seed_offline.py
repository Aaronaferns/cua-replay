"""Fixture identities and the seed logic, offline: no Docker, a fake REST server."""

from __future__ import annotations

import re
from decimal import Decimal

import dolictl
import pytest
import seed
from dolirest import Rest, derived_password, load_fixtures, outstanding_of, parse_amount, state_snapshot
from fake_dolibarr import FakeDolibarr

from cua.config import Settings

ADMIN_KEY = "admin-key-for-tests"


def test_fixtures_have_known_distinct_identities():
    fixtures = load_fixtures()
    codes = [c.code for c in fixtures.customers]
    assert len(set(codes)) == len(codes)
    assert all(len(code) >= 11 for code in codes)  # Dolibarr's default numbering refuses shorter codes
    with_invoices = [c for c in fixtures.customers if c.invoices]
    assert len(with_invoices) >= 2 and len({c.outstanding for c in with_invoices}) == len(with_invoices)
    assert fixtures.customer("alpha").outstanding == Decimal("199.75")
    assert fixtures.customer("beta").outstanding == Decimal("355.20")
    # the demo customer that makes the missing code a near miss, and no seeded code collides with either
    assert fixtures.near_miss_existing_code.startswith(fixtures.near_miss_missing_code)
    assert not any(code.startswith(fixtures.near_miss_missing_code) for code in codes)


@pytest.mark.parametrize("text,expected", [
    ("75.37 €", "75.37"),
    ("Current outstanding bill 75.37 €", "75.37"),
    ("$1,075.37", "1075.37"),
    ("1 075,37 €", "1075.37"),
    ("1.075,37 €", "1075.37"),
    ("0.00 €", "0.00"),
    ("-12.50 €", "-12.50"),
])
def test_parse_amount_reads_the_last_amount(text, expected):
    assert parse_amount(text) == Decimal(expected)


def test_derived_password_is_stable_and_per_purpose():
    cfg = Settings(cua_run_password="run-secret")
    assert derived_password(cfg, "readonly").get_secret_value() == derived_password(cfg, "readonly").get_secret_value()
    assert derived_password(cfg, "readonly").get_secret_value() != derived_password(cfg, "other").get_secret_value()
    assert "run-secret" not in repr(derived_password(cfg, "readonly"))


@pytest.fixture
def fake(monkeypatch):
    fake = FakeDolibarr()
    base = fake.start()
    port = int(re.search(r":(\d+)/", base).group(1))
    cfg = Settings(dolibarr_port=port, dolibarr_api_key=ADMIN_KEY, cua_run_login="cua_operator",
                   cua_run_password="run-pass-for-tests", cua_noperm_password="noperm-pass-for-tests",
                   dolibarr_db_root_password="unused")

    def fake_sql(settings, statements, **kwargs):
        if m := re.fullmatch(r"UPDATE llx_user SET api_key = '([^']+)' WHERE rowid = (\d+);", statements):
            fake.keys[m.group(1)] = fake.users[int(m.group(2))]["login"]
            return ""
        if m := re.search(r"SELECT 1, (\d+),", statements):
            login = fake.users[int(m.group(1))]["login"]
            fake.grant(login, "write" if login == cfg.cua_run_login else "read")
            return ""
        if "SELECT d.module" in statements:
            return "".join("\t".join((mod, perm, sub or "")) + "\n"
                           for mod, perm, sub in seed.RUN_RIGHTS + seed.READONLY_RIGHTS)
        raise AssertionError(f"unexpected SQL: {statements}")

    monkeypatch.setattr(dolictl, "sql", fake_sql)
    yield fake, cfg
    fake.stop()


def test_seed_creates_known_state_and_replaces_the_demo_key(fake):
    fake, cfg = fake
    fixtures = seed.seed(cfg)
    admin = Rest.admin(cfg)
    assert fake.keys.get("demokey") is None and fake.keys[ADMIN_KEY] == "admin"
    for customer in fixtures.customers:
        found = admin.listing("/thirdparties", sqlfilters=f"(t.code_client:=:'{customer.code}')")
        assert len(found) == 1
        assert outstanding_of(admin, found[0]["id"]) == customer.outstanding
    assert {u["login"] for u in fake.users.values()} >= {"cua_operator", "cua_readonly", "noperm"}
    assert any(b["ref"] == fixtures.bank_ref for b in fake.banks.values())
    # validated, not draft
    assert {i["statut"] for i in fake.invoices.values()} == {"1"}


def test_seed_is_idempotent(fake):
    fake, cfg = fake
    seed.seed(cfg)
    before = state_snapshot(Rest.admin(cfg))
    seed.seed(cfg)
    assert state_snapshot(Rest.admin(cfg)) == before
    created = [path for method, path in fake.calls if method == "POST" and path in ("/thirdparties", "/users")]
    assert created.count("/thirdparties") == 3 and created.count("/users") == 2  # nothing created twice
    assert len(fake.invoices) == 4 and len(fake.thirdparties) == 4  # 3 seeded + the demo customer


def test_seed_falls_back_to_the_database_when_rest_ignores_the_key(fake, monkeypatch):
    fake, cfg = fake
    fake.api_key_put_works = False
    statements = []

    def sql_with_key_update(settings, text, **kwargs):
        if text.startswith("UPDATE llx_user SET api_key") and "login = 'admin'" in text:
            statements.append(text)
            fake.keys = {k: v for k, v in fake.keys.items() if v != "admin"}
            fake.keys[ADMIN_KEY] = "admin"
            return ""
        return dolictl_sql(settings, text, **kwargs)

    dolictl_sql = dolictl.sql
    monkeypatch.setattr(dolictl, "sql", sql_with_key_update)
    seed.seed(cfg)
    assert len(statements) == 1 and "demokey" not in statements[0]


def test_seed_refuses_when_the_demo_key_would_stay_valid(fake):
    fake, cfg = fake
    fake.keys[ADMIN_KEY] = "admin"  # configured key already works ...
    with pytest.raises(seed.SeedError, match="demo key"):
        seed.ensure_admin_key(cfg)  # ... but `demokey` is still accepted too


def test_seed_error_messages_do_not_carry_secrets(fake):
    fake, cfg = fake
    fake.rights["noperm"] = "read"  # the no-rights user is not refused: the access check must fail
    with pytest.raises(seed.SeedError) as e:
        seed.seed(cfg)
    text = str(e.value)
    assert "noperm-pass-for-tests" not in text and "run-pass-for-tests" not in text and ADMIN_KEY not in text
    assert "no-rights user" in text
