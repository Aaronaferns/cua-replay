"""Human-action capture and the human request log, against a local site in a real Chromium."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cua.artifact.lock import load_profile
from cua.artifact.schema import RoutePattern
from cua.config import REPO_ROOT
from cua.control.capture import (
    CAPTURE_JS,
    CaptureEvent,
    RequestLog,
    commit_hit_sink,
    evidence_sink,
    install_capture,
    parse_payload,
)
from cua.control.lease import LeaseToken
from cua.evidence.writer import RunBundleWriter
from cua.safety.redact import Redactor

CAPTCHA_VALUE = "xK7pQ2"
PASSWORD = "hunter2-pass"
TOKEN = "SECRETTOKEN1234"


def profile_with_pay_commit():
    base = load_profile(REPO_ROOT / "apps" / "dolibarr" / "profile.json")
    return base.model_copy(
        update={"commit_routes": [RoutePattern(method="POST", path="/pay.php", body={"action": "confirm_paiement"})]}
    )


async def drive_login_and_pay(page, origin: str) -> None:
    await page.goto(f"{origin}/login.php")
    await page.get_by_role("textbox", name="Login").fill("operator")  # fill: no key events, a `change` on blur
    await page.get_by_role("textbox", name="Password").fill(PASSWORD)
    await page.get_by_role("textbox", name="Security code").click()
    await page.keyboard.type(CAPTCHA_VALUE)
    await page.get_by_role("combobox", name="Language").select_option("de")
    await page.keyboard.press("Enter")  # implicit submit: enter + submit + a full-page POST
    await page.wait_for_url("**/login.php")
    await page.get_by_role("textbox", name="Note").click()
    await page.keyboard.type("my private note")
    await page.get_by_role("button", name="Save note").click()  # a POST that is not a commit
    await page.get_by_role("textbox", name="Amount").click()
    await page.keyboard.type("123.45")
    await page.get_by_role("button", name="Pay").click()  # the commit POST
    await page.wait_for_load_state()


async def test_human_actions_are_captured_across_navigations_with_values_redacted(browser, site):
    context = await browser.new_context()
    page = await context.new_page()
    events: list[CaptureEvent] = []
    await install_capture(context, on_event=events.append)

    await drive_login_and_pay(page, site.origin)
    await page.wait_for_timeout(200)
    await context.close()

    kinds = [e.kind for e in events]
    for kind in ("click", "change", "enter", "submit"):
        assert kind in kinds, kinds
    assert all(e.actor == "human" for e in events)
    assert {e.method for e in events if e.kind == "submit"} == {"post"}  # captured after the full-page loads too
    assert {e.path for e in events} >= {"/login.php"}

    dump = json.dumps([e.model_dump(mode="json") for e in events])
    for secret in (CAPTCHA_VALUE, PASSWORD, "my private note", "123.45", "operator"):
        assert secret not in dump  # typed values never leave the page, the CAPTCHA included
    assert TOKEN not in dump and "?" not in "".join(e.path or "" for e in events)
    captcha = [e for e in events if e.kind == "change" and e.label == "Security code"]
    assert captcha and all(e.value == "[REDACTED]" for e in captcha)
    assert any(e.kind == "click" and e.tag == "input" and e.text == "Pay" for e in events)
    assert any(e.kind == "submit" and e.action == "/pay.php" and "amount" in e.fields for e in events), [
        (e.action, e.fields) for e in events if e.kind == "submit"
    ]


async def test_capture_reaches_a_page_that_is_already_open(browser, site):
    context = await browser.new_context()
    page = await context.new_page()
    await page.goto(f"{site.origin}/login.php")  # the human takes over a page that is already showing
    events: list[CaptureEvent] = []
    await install_capture(context, on_event=events.append)
    await page.get_by_role("textbox", name="Security code").click()
    await page.keyboard.type(CAPTCHA_VALUE)
    await page.get_by_role("textbox", name="Login").click()  # blur: change
    await page.wait_for_timeout(200)
    await context.close()
    assert [e.kind for e in events].count("click") == 2
    assert any(e.kind == "change" and e.label == "Security code" for e in events)
    assert CAPTCHA_VALUE not in json.dumps([e.model_dump(mode="json") for e in events])


async def test_inactive_and_closed_captures_forward_nothing(browser, site):
    context = await browser.new_context()
    page = await context.new_page()
    events: list[CaptureEvent] = []
    human = {"on": False}
    installation = await install_capture(context, on_event=events.append, active=lambda: human["on"])
    await page.goto(f"{site.origin}/login.php")
    await page.get_by_role("textbox", name="Login").click()
    await page.wait_for_timeout(150)
    assert events == [] and installation.dropped >= 1  # automation holds the lease: nothing is a human action
    human["on"] = True
    await page.get_by_role("textbox", name="Password").click()
    await page.wait_for_timeout(150)
    assert [e.kind for e in events] == ["click"]
    installation.close()
    await page.get_by_role("textbox", name="Login").click()
    await page.wait_for_timeout(150)
    assert len(events) == 1
    await context.close()


async def test_a_failing_sink_does_not_break_the_page(browser, site):
    context = await browser.new_context()
    page = await context.new_page()

    def boom(event):
        raise RuntimeError("sink failure")

    await install_capture(context, on_event=boom)
    await page.goto(f"{site.origin}/login.php")
    await page.get_by_role("textbox", name="Login").click()
    await page.get_by_role("textbox", name="Login").fill("still works")
    assert await page.get_by_role("textbox", name="Login").input_value() == "still works"
    await context.close()


# ------------------------------------------------------------------------------------------ the python-side guard


def test_a_forged_payload_cannot_smuggle_a_value():
    forged = json.dumps({"kind": "change", "tag": "input", "value": "STOLEN-VALUE", "actor": "agent",
                         "label": "x" * 500, "extra": "STOLEN-EXTRA", "fields": ["a", 5, "STOLEN-FIELD"]})
    event = parse_payload(forged)
    assert event is not None and event.actor == "human" and event.value == "[REDACTED]"
    assert len(event.label or "") == 80
    assert "STOLEN-VALUE" not in event.model_dump_json() and "STOLEN-EXTRA" not in event.model_dump_json()
    assert event.fields == ["a", "STOLEN-FIELD"]  # field *names* are page structure; non-strings are dropped


@pytest.mark.parametrize("payload", [None, 5, "not json", "[]", json.dumps({"kind": "keypress"}), json.dumps({})])
def test_malformed_or_unknown_payloads_are_dropped(payload):
    assert parse_payload(payload) is None


def test_click_events_carry_no_value():
    event = parse_payload(json.dumps({"kind": "click", "tag": "input", "value": "STOLEN"}))
    assert event is not None and event.value is None


def test_the_script_never_reads_field_values_except_to_measure_nothing():
    # the only `.value` read is a submit/button input's own label; typed fields are only ever reported as [REDACTED]
    reads = [line.strip() for line in CAPTURE_JS.splitlines() if ".value" in line and "REDACTED" not in line]
    assert reads == ["else if (isButtonInput(el)) o.text = clip(el.value, 40);"]


# -------------------------------------------------------------------------------------------------- request log


async def test_request_log_flags_commit_routes_without_logging_queries(browser, site):
    context = await browser.new_context()
    page = await context.new_page()
    hits: list = []
    log = RequestLog(profile_with_pay_commit(), on_commit=hits.append)
    log.attach(context)

    await drive_login_and_pay(page, site.origin)
    await context.close()

    assert any(e.method == "POST" and e.url.endswith("/pay.php") and e.commit for e in log.entries)
    assert [e.url.rsplit("/", 1)[-1] for e in log.commit_hits] == ["pay.php"]  # the note POST is logged, not flagged
    assert any(e.url.endswith("/note.php") and not e.commit for e in log.entries)
    assert hits == log.commit_hits
    assert TOKEN not in json.dumps([e.model_dump(mode="json") for e in log.entries])
    assert all("?" not in e.url and e.actor == "human" for e in log.entries)


async def test_request_log_treats_an_unreadable_body_as_possibly_committing(browser, site):
    context = await browser.new_context()
    page = await context.new_page()
    log = RequestLog(profile_with_pay_commit())
    log.attach(context)
    await page.goto(f"{site.origin}/login.php")
    await page.evaluate(
        "fetch('/pay.php', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'})"
    )
    await page.wait_for_timeout(300)
    await context.close()
    assert [e.url.rsplit("/", 1)[-1] for e in log.commit_hits] == ["pay.php"]  # fields unknown: treated as a commit


async def test_request_log_detach_and_active_gate(browser, site):
    context = await browser.new_context()
    page = await context.new_page()
    on = {"human": False}
    log = RequestLog(profile_with_pay_commit(), active=lambda: on["human"])
    log.attach(context)
    await page.goto(f"{site.origin}/login.php")
    assert log.entries == []  # automation's own traffic is not the human's
    on["human"] = True
    await page.goto(f"{site.origin}/list.php")
    assert [e.url.rsplit("/", 1)[-1] for e in log.entries] == ["list.php"]
    log.detach()
    await page.goto(f"{site.origin}/login.php")
    assert len(log.entries) == 1
    await context.close()


async def test_sinks_write_human_events_under_the_lease(tmp_path: Path, browser, site):
    writer = RunBundleWriter("run-c", root=tmp_path, redactor=Redactor([CAPTCHA_VALUE, PASSWORD]))
    lease = lambda: LeaseToken(holder="human:op1", generation=2)  # noqa: E731
    context = await browser.new_context()
    page = await context.new_page()
    await install_capture(context, on_event=evidence_sink(writer, lease))
    log = RequestLog(profile_with_pay_commit(), on_commit=commit_hit_sink(writer, lease))
    log.attach(context)
    await drive_login_and_pay(page, site.origin)
    await page.wait_for_timeout(200)
    await context.close()

    events = [json.loads(line) for line in (writer.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events and all(e["actor"] == "human" and e["lease"] == {"holder": "human:op1", "generation": 2} for e in events)
    assert {"action", "commit_attempt"} <= {e["kind"] for e in events}
    assert [e["seq"] for e in events] == list(range(len(events)))
    blob = (writer.run_dir / "events.jsonl").read_text(encoding="utf-8")
    assert CAPTCHA_VALUE not in blob and PASSWORD not in blob and TOKEN not in blob
