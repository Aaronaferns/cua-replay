"""RunResult: what a replay returns to its caller.

Owner: Agent B. Seeded by the orchestrator from the build plan so that C (console, evidence, interventions) and the
CLI can code against it; B owns evolution through M4.

Exactly four terminal statuses. Policy denial is `failure` with category `policy` and `approvable`, not a fifth
status. Recoveries and drift appear on every status. `retry_safe` is computed from what happened, never declared.
Sensitive observed values are masked before they enter a result.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

Status = Literal["success", "business_outcome", "failure", "indeterminate"]
FailureCategory = Literal[
    "caller", "policy", "locator", "state", "data", "app", "transient", "control", "commit"
]
CommitState = Literal["none", "attempted", "confirmed", "rejected", "unknown"]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class ObservedState(_M):
    state: str | None  # classified state id, "unknown" or "ambiguous"
    url: str  # masked
    near_misses: list[str] = []


class Failure(_M):
    code: str  # e.g. TARGET_AMBIGUOUS, IDENTITY_MISMATCH, TRANSIENT_EXHAUSTED, APP_ERROR, COMMIT_UNKNOWN
    category: FailureCategory
    step_id: str | None
    step_intent: str | None
    expected: str | None
    observed: ObservedState | None
    detail: str  # safe text: no raw input or output values
    evidence: list[str] = []  # bundle-relative paths
    approvable: bool = False


class Recovery(_M):
    handler: str
    state: str
    step_id: str | None
    at_ms: int


class DriftWarning(_M):
    step_id: str
    target: str
    failed_strategies: list[str]
    used_strategy: str


class SideEffects(_M):
    commit_state: CommitState = "none"
    by: Literal["automation", "human"] | None = None
    request_id: str | None = None


class RunResult(_M):
    run_id: str
    capability: str
    version: str
    lock_hash: str | None
    status: Status
    outcome: str  # "success" | declared business outcome code | failure code
    outputs: dict[str, Any] = {}
    failure: Failure | None = None
    recoveries: list[Recovery] = []
    drift: list[DriftWarning] = []
    side_effects: SideEffects = SideEffects()
    retry_safe: bool
    interventions: list[str] = []
    timings: dict[str, int] = {}
