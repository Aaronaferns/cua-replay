"""Evidence events and the evidence writer interface.

Owner: Agent C. Seeded by the orchestrator with the shared types only.

`events.jsonl` uses one schema for every actor. All persistence goes through one `EvidenceWriter`, which redacts
before anything touches disk; no other code writes run output files.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from cua.control.lease import LeaseToken
from cua.safety.policy import PolicyDecision

EventActor = Literal["agent", "replay", "human", "system"]


class EvidenceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    ts: datetime
    seq: int
    run_id: str
    actor: EventActor
    kind: str  # e.g. "observe", "action", "policy", "state", "recovery", "drift", "commit_attempted" (replay),
    # "commit_attempt" (human capture), "escalated", "handoff_ended", "lease", "intervention", "result", "fault"
    step_id: str | None = None
    state: str | None = None
    action: dict[str, Any] | None = None  # tokens, never raw input values
    rationale: str | None = None  # agent only: the short stated reason, never hidden reasoning
    policy: PolicyDecision | None = None
    lease: LeaseToken | None = None
    detail: dict[str, Any] = {}
    duration_ms: int | None = None


class EvidenceWriter(Protocol):
    """The single persistence choke point for a run bundle. Every method redacts before writing."""

    run_dir: Path

    def event(self, event: EvidenceEvent) -> None: ...
    def json(self, relpath: str, obj: Any) -> Path: ...
    def text(self, relpath: str, text: str) -> Path: ...
    def image(self, relpath: str, png: bytes) -> Path: ...
