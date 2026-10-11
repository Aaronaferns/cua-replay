"""Smoke tests for the seeded shared contracts: they import, validate and keep their key invariants."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from cua.agent.actions import AgentAction
from cua.artifact.schema import Strategy
from cua.cli import build_parser
from cua.config import Settings
from cua.control.lease import ControlLease
from cua.replay.result import RunResult


def test_action_schema_is_closed_and_discriminated():
    action = TypeAdapter(AgentAction).validate_python({"kind": "click", "ref": "e12", "rationale": "open the card"})
    assert action.kind == "click"
    with pytest.raises(ValidationError):
        TypeAdapter(AgentAction).validate_python({"kind": "run_js", "code": "1", "rationale": "x"})


def test_validation_errors_hide_input_values():
    with pytest.raises(ValidationError) as e:
        Strategy(kind="role", why="w", matched_at_record="1 of 1", unexpected="CU1910-00021")
    assert "CU1910-00021" not in str(e.value)


def test_effect_has_no_write():
    with pytest.raises(ValidationError):
        TypeAdapter(Strategy).validate_python({"kind": "write", "why": "w", "matched_at_record": "x"})
    from typing import get_args

    from cua.artifact.schema import Effect

    assert set(get_args(Effect)) == {"none", "read", "commit"}


def test_run_result_has_four_statuses():
    from typing import get_args

    from cua.replay.result import Status

    assert set(get_args(Status)) == {"success", "business_outcome", "failure", "indeterminate"}
    r = RunResult(run_id="r", capability="c", version="1.0.0", lock_hash=None, status="success",
                  outcome="success", retry_safe=True)
    assert r.recoveries == [] and r.drift == []


def test_lease_token_matches_lease():
    lease = ControlLease(holder="automation", generation=3, reason="start", since=datetime.now(UTC))
    assert lease.token().generation == 3


def test_secrets_do_not_print():
    s = Settings(dolibarr_admin_password="hunter2-not-real")
    assert "hunter2" not in repr(s) and s.secret_values() == ["hunter2-not-real"]


def test_cli_builds_without_any_commands_yet():
    build_parser()
