"""Redaction: known values in every encoding they take, patterns for things not known in advance, partial masks."""

from __future__ import annotations

import html
import json
from urllib.parse import quote, quote_plus

import pytest
from pydantic import SecretStr

from cua.config import Settings
from cua.safety.redact import Redactor

SECRET = "pa55/w0rd&<x>\"'é"  # every encoding differs from the raw form


def test_redactor_known_values_and_variants():
    r = Redactor([SECRET])
    forms = {
        "raw": SECRET,
        "url": quote(SECRET, safe=""),
        "url_default_safe": quote(SECRET),
        "url_plus": quote_plus(SECRET),
        "url_lower_hex": quote(SECRET, safe="").lower(),
        "url_double": quote(quote(SECRET, safe=""), safe=""),
        "html": html.escape(SECRET),
        "html_no_quote": html.escape(SECRET, quote=False),
        "json": json.dumps(SECRET)[1:-1],
        "json_unicode": json.dumps(SECRET, ensure_ascii=False)[1:-1],
        "json_php_slash": json.dumps(SECRET)[1:-1].replace("/", "\\/"),
        "upper": SECRET.upper(),
        "lower": SECRET.lower(),
    }
    for name, form in forms.items():
        out = r.text(f"before {form} after")
        assert form not in out, name
        assert out == "before [REDACTED:input] after", name


def test_known_value_inside_a_larger_token_is_still_redacted():
    out = Redactor(["hunter2-secret"]).text("url=/x?p=prefix-hunter2-secret-suffix")
    assert "hunter2-secret" not in out


def test_settings_secrets_are_all_known_and_never_printed():
    s = Settings(
        anthropic_api_key="sk-ant-NOTREAL-1234567890",
        dolibarr_admin_password="admin-pass-NOTREAL",
        dolibarr_api_key="api-key-NOTREAL-abcdef",
        cua_run_password="run-pass-NOTREAL",
    )
    r = Redactor.from_settings(s)
    blob = " | ".join(s.secret_values())
    assert r.known_value_count == 4
    out = r.text(blob)
    for value in s.secret_values():
        assert value not in out
    assert "NOTREAL" not in repr(r)


def test_secretstr_values_are_accepted_and_labelled():
    r = Redactor()
    assert r.add(SecretStr("run-supplied-value"), label="amount")
    assert r.text("x run-supplied-value y") == "x [REDACTED:amount] y"


def test_short_values_are_not_matched_as_substrings():
    r = Redactor()
    assert r.add("ab") is False
    assert r.text("abc ab") == "abc ab"


def test_partial_mask_keeps_the_last_four():
    r = Redactor()
    r.add("4111111111111111", keep_last=4)
    out = r.text("card 4111111111111111 / 4111-1111 not matched")
    assert out.startswith("card ************1111 /")
    assert "4111111111111111" not in out
    with pytest.raises(ValueError):
        r.add("short-value", keep_last=8)  # would reveal most of the value


def test_ssn_pattern_keeps_only_the_last_four():
    assert Redactor().text("ssn 123-45-6789.") == "ssn ***-**-6789."


def test_email_pattern():
    out = Redactor().text("contact john.doe+x@example.co.uk now")
    assert "john.doe" not in out and "[REDACTED:email]" in out


@pytest.mark.parametrize(
    ("line", "leak"),
    [
        ("Cookie: DOLSESSID_abc=0123456789abcdef; other=1", "0123456789abcdef"),
        ("Set-Cookie: DOLSESSID_abc=0123456789abcdef; Path=/; HttpOnly", "0123456789abcdef"),
        ("authorization: Bearer abcdefghijklmnop", "abcdefghijklmnop"),
        ("see https://x/y?token=abcd1234efgh&a=1", "abcd1234efgh"),
        ("href='/c.php?action=confirm_x&newtoken=feedfacecafebeef&id=2'", "feedfacecafebeef"),
        ('<input type="hidden" name="token" value="deadbeefdeadbeef">', "deadbeefdeadbeef"),
        ('<input value="deadbeefdeadbeef" type="hidden" name="newtoken">', "deadbeefdeadbeef"),
        ('{"access_token": "eyJhbGciOi.payload.sig", "x": 1}', "eyJhbGciOi"),
        ("PHPSESSID=0a1b2c3d4e5f6a7b8c9d", "0a1b2c3d4e5f6a7b8c9d"),
        ("DOLAPIKEY: 5ecretK3y", "5ecretK3y"),
    ],
)
def test_patterns_redact_unknown_credentials(line, leak):
    out = Redactor().text(line)
    assert leak not in out and "[REDACTED" in out


def test_ordinary_text_is_untouched():
    text = "Current outstanding bill: 1,234.56 EUR for CU1910-00021 (invoice FA2310-0004)"
    assert Redactor(["unrelated-secret"]).text(text) == text


def test_data_redacts_strings_keys_and_sensitive_fields():
    r = Redactor(["typed-value-1"])
    obj = {
        "action": {"kind": "type", "text": "{{customer_code}}", "echo": "typed-value-1"},
        "headers": {"Cookie": "a=b", "Authorization": "Bearer xxxxxxxxxxxx", "Accept": "text/html"},
        "typed-value-1": "key leak",
        "nested": [{"password": "x"}, ["typed-value-1"], 42, None, True],
        "token_example": "{{customer_code}}",
    }
    out = r.data(obj)
    dumped = json.dumps(out)
    assert "typed-value-1" not in dumped
    assert out["action"]["text"] == "{{customer_code}}"  # input tokens are evidence, not secrets
    assert out["headers"] == {"Cookie": "[REDACTED]", "Authorization": "[REDACTED]", "Accept": "text/html"}
    assert out["nested"][0] == {"password": "[REDACTED]"}
    assert out["nested"][2:] == [42, None, True]
    assert out["token_example"] == "{{customer_code}}"


def test_numbers_equal_to_a_known_value_are_redacted():
    r = Redactor(["123456"])
    assert r.data({"n": 123456, "m": 7}) == {"n": "[REDACTED:input]", "m": 7}


def test_values_added_later_apply_to_later_calls():
    r = Redactor()
    assert r.text("order 98765432") == "order 98765432"
    r.add("98765432")
    assert r.text("order 98765432") == "order [REDACTED:input]"


def test_text_is_idempotent():
    r = Redactor(["typed-value-1"])
    once = r.text("Cookie: a=b typed-value-1 123-45-6789 a@b.co")
    assert r.text(once) == once
