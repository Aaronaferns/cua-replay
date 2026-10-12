"""The intervention request: a value-free JSON record with a screenshot path, the lease and an SLA deadline."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from cua.control.interventions import (
    DEFAULT_SLA_SECONDS,
    HUMAN_ONLY_LOGIN,
    InterventionRequest,
    write_intervention,
)
from cua.control.lease import LeaseToken
from cua.evidence.writer import RunBundleWriter
from cua.safety.redact import Redactor

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8


def make(**over) -> InterventionRequest:
    fields = dict(
        id="int-1", run_id="run-1", capability="dolibarr.lookup_customer_outstanding", version="1.0.0",
        reason=HUMAN_ONLY_LOGIN, url="http://localhost:8087/index.php?token=abc&user=bob#frag",
        lease=LeaseToken(holder="none", generation=1), now=NOW, step_id="s1", classified_state="login_captcha",
        expected_state="home", screenshot="interventions/int-1.png", effect_so_far="read",
    )
    fields.update(over)
    return InterventionRequest.create(**fields)


def test_the_request_has_every_field_the_plan_lists_and_a_deadline():
    r = make()
    data = r.model_dump(mode="json")
    assert data["schema"] == "cua.intervention/v1"
    assert data["capability"] and data["step_id"] == "s1" and data["reason"] == "HUMAN_ONLY_LOGIN"
    assert data["classified_state"] == "login_captcha" and data["expected_state"] == "home"
    assert data["screenshot"] == "interventions/int-1.png" and data["effect_so_far"] == "read"
    assert data["lease"] == {"holder": "none", "generation": 1}
    assert r.created_at == NOW and r.sla_deadline == NOW + timedelta(seconds=DEFAULT_SLA_SECONDS)
    assert InterventionRequest.model_validate_json(r.model_dump_json()) == r  # round-trips through its own JSON


def test_the_url_never_carries_a_query_fragment_or_userinfo():
    r = make(url="http://user:pw@localhost:8087/index.php?token=abc&user=bob#frag")
    assert r.url == "http://localhost:8087/index.php"
    assert "token" not in r.model_dump_json() and "pw" not in r.url


def test_overdue_is_a_pure_deadline_check_and_no_sla_never_expires():
    r = make(sla_seconds=60)
    assert not r.overdue(NOW + timedelta(seconds=60)) and r.overdue(NOW + timedelta(seconds=61))
    assert not make(sla_seconds=None).overdue(NOW + timedelta(days=365))


@pytest.mark.parametrize(
    "bad",
    [{"reason": "human only login"}, {"reason": "ab"}, {"id": "../x"}, {"created_at": datetime(2026, 1, 1)},
     {"sla_seconds": 0}],
)
def test_invalid_requests_are_refused_without_echoing_inputs(bad):
    fields = make().model_dump()
    fields.update(bad)
    with pytest.raises(ValidationError) as caught:
        InterventionRequest(**fields)
    assert "input_value" not in str(caught.value)


def test_a_deadline_before_creation_is_refused():
    with pytest.raises(ValidationError):
        InterventionRequest(**{**make().model_dump(), "sla_deadline": NOW - timedelta(seconds=1)})


def test_write_intervention_persists_json_and_logs_the_event(tmp_path):
    writer = RunBundleWriter("run-1", root=tmp_path, redactor=Redactor())
    writer.image("interventions/int-1.png", PNG)
    request = make()
    relpath = write_intervention(writer, request)
    assert relpath == "interventions/int-1.json"
    stored = json.loads((writer.run_dir / relpath).read_text(encoding="utf-8"))
    assert stored["reason"] == "HUMAN_ONLY_LOGIN" and stored["url"] == "http://localhost:8087/index.php"
    events = [json.loads(line) for line in (writer.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[-1]["kind"] == "intervention" and events[-1]["actor"] == "system"
    assert events[-1]["lease"] == {"holder": "none", "generation": 1}
    assert events[-1]["detail"]["request"] == relpath and events[-1]["step_id"] == "s1"
