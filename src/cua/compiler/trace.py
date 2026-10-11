"""RecordedTrace: the handoff contract from discovery (Agent A, producer) to the compiler (Agent B, consumer).

Owner: Agent A, with B as required reviewer: any field change needs both and goes through the orchestrator.
Seeded by the orchestrator for M2; expect A and B to refine it before the first real trace is committed.

The persisted trace is already redacted, and the compiler must work from the persisted trace alone: compiling the
committed trace reproduces the committed artifact byte-for-byte. So everything the compiler needs (which input token
went where, which elements were acted on and how they can be found, where an input value was displayed after
navigation) is captured at record time as structure, not as raw values.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cua.agent.actions import AgentAction
from cua.artifact.schema import Effect, FieldSpec, Fingerprint, Strategy
from cua.safety.policy import PolicyDecision


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class LocatorCandidate(_M):
    rank: int  # 1 = role+name, 2 = same_row, 3 = table_cell, 4 = scoped, 5 = normalize(), 6 = css
    strategy: Strategy
    match_count: int
    same_element: bool  # resolved element is the element the ref pointed at


class TargetRecord(_M):
    """An element the agent acted on or cited, captured while its ref was live."""

    ref: str
    role: str
    name: str
    fingerprint: Fingerprint
    candidates: list[LocatorCandidate]  # only kept candidates: match_count == 1 and same_element


class TokenSighting(_M):
    """An input token's value displayed on screen after an action (tagged before redaction)."""

    token: str
    target: TargetRecord


class ScreenRecord(_M):
    epoch: int
    url: str  # redacted (CSRF tokens and record ids masked where sensitive)
    title: str
    heading: str | None = None  # first main-content heading
    dialog: str | None = None
    snapshot: str  # redacted scoped AI snapshot


class RecordedAction(_M):
    seq: int
    action: AgentAction  # as proposed, with tokens unsubstituted
    tokens: list[str] = []  # input tokens referenced by the action's arguments
    effect: Effect  # classified by the guard path, not by the model
    policy: PolicyDecision
    status: Literal["performed", "blocked", "error"]
    error: str | None = None
    target: TargetRecord | None = None
    pre: ScreenRecord
    post: ScreenRecord | None = None
    sightings: list[TokenSighting] = []
    started_at: datetime
    duration_ms: int


class RecordedTrace(_M):
    schema_: Literal["cua.trace/v0"] = Field("cua.trace/v0", alias="schema")
    run_id: str
    app: str
    goal: str  # with tokens, never raw values
    model: str
    started_at: datetime
    inputs: dict[str, FieldSpec]
    outputs: dict[str, FieldSpec]
    entry_url: str
    actions: list[RecordedAction]
    outcome: Literal["done", "stopped"]
    stop_reason: str | None = None
    done_evidence: list[TargetRecord] = []
