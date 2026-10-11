"""Policy: the single automated-action gate.

Owner: Agent C. Seeded by the orchestrator with the shared types only.

Every automated action (discovery, replay, handlers) is described as an `ActionRequest` and checked by the `Guard`
*before* the surface touches the UI. Unknown means deny. A denied action raises `PolicyDenied` and leaves the UI
untouched. Policy denial reaches callers as `failure` with category `policy` and `approvable`, never as its own status.
"""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from cua.artifact.schema import Effect
from cua.control.lease import LeaseToken

ActionKind = Literal["click", "type", "select", "press", "navigate", "extract"]
Actor = Literal["agent", "replay", "handler"]


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    kind: ActionKind
    actor: Actor
    page_url: str  # where the action happens
    target_url: str | None = None  # navigate: destination; link clicks: href when known
    effect: Effect
    lease: LeaseToken
    approval_id: str | None = None  # single-use approval bound to (action, generation), commit only


class PolicyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    rule: str  # which rule decided, e.g. "origin_allowlist", "action_type", "effect_ceiling", "lease_generation"
    reason: str  # safe to log: never contains input values
    approvable: bool = False


class PolicyDenied(Exception):
    def __init__(self, decision: PolicyDecision):
        super().__init__(f"policy denied ({decision.rule}): {decision.reason}")
        self.decision = decision


class Guard(Protocol):
    def check(self, request: ActionRequest) -> PolicyDecision: ...
