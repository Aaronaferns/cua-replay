"""Comparing a RunResult with a suite case, and the validation summary approval relies on.

Pure: no browser, no target, no oracle. The validation command resolves ground truth and passes it in.

Imports: artifact and this package.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cua.artifact.suite import Case
from cua.replay.result import RunResult


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class CaseRun(_M):
    case_id: str
    repeat: int
    run_id: str
    passed: bool
    status: str | None  # None when the run could not be made at all
    outcome: str | None
    mismatches: list[str] = []
    duration_ms: int = 0


class Summary(_M):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, validate_by_name=True,
                              validate_by_alias=True, serialize_by_alias=True)

    schema_: Literal["cua.validation/v1"] = Field("cua.validation/v1", alias="schema")
    run_id: str
    capability: str
    version: str
    lock_hash: str
    repeat: int
    started_at: datetime
    passed: bool
    cases: list[CaseRun]


def compare(case: Case, result: RunResult, truth: dict[str, str], declared_outputs: list[str]) -> list[str]:
    """Why the run does not match the case (empty = it does). `truth` maps an output name to the oracle's value."""
    e = case.expect
    bad: list[str] = []
    if result.status != e.status:
        bad.append(f"status {result.status!r}, expected {e.status!r}")
    if result.outcome != e.outcome:
        bad.append(f"outcome {result.outcome!r}, expected {e.outcome!r}")
    if result.failure is not None and e.status != "failure":
        bad.append(f"unexpected failure {result.failure.code}")
    expected = dict(e.outputs)
    for name, value in truth.items():
        if name in expected and expected[name] != value:
            bad.append(f"the case's expected {name} differs from the ground truth")
        expected.setdefault(name, value)
    for name, value in expected.items():
        got = result.outputs.get(name)
        if got is None:
            bad.append(f"output {name} missing")
        elif str(got) != value:
            bad.append(f"output {name} is {got!r}, expected {value!r}")
    if e.status == "success" and set(result.outputs) != set(declared_outputs):
        bad.append(f"outputs {sorted(result.outputs)}, declared {sorted(declared_outputs)}")
    if sorted(r.handler for r in result.recoveries) != sorted(e.recoveries):
        bad.append(f"recoveries {sorted(r.handler for r in result.recoveries)}, expected {sorted(e.recoveries)}")
    if result.drift and not e.allow_drift:
        bad.append(f"{len(result.drift)} target(s) needed a fallback strategy")
    return bad


def check_validation_runs(
    validation_dir: Path, run_ids: list[str], *, capability: str, version: str, lock_hash: str
) -> list[str]:
    """Problems with the validation runs an approval cites: each must exist, have passed, and have run exactly the
    capability, version and lock hash being approved (a profile edited after validation does not count)."""
    problems: list[str] = []
    for run_id in run_ids:
        path = validation_dir / run_id / "summary.json"
        if not path.is_file():
            problems.append(f"validation run {run_id!r} has no summary under {validation_dir}")
            continue
        try:
            summary = Summary.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except ValueError:
            problems.append(f"validation run {run_id!r} has an unreadable summary")
            continue
        if not summary.passed:
            problems.append(f"validation run {run_id!r} did not pass")
        if (summary.capability, summary.version) != (capability, version):
            problems.append(f"validation run {run_id!r} was for {summary.capability}@{summary.version}")
        elif summary.lock_hash != lock_hash:
            problems.append(f"validation run {run_id!r} was for a different lock hash (edited since)")
    return problems
