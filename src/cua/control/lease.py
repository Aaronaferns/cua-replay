"""Control lease: who may act on the live session right now.

Owner: Agent C (safety + control). Seeded by the orchestrator with the shared types only.

Every automated action carries a `LeaseToken` (holder + generation it believes it holds). The policy guard rejects
the action unless the token matches the current lease exactly. Each transfer increments `generation`, so a decision
made before a handoff cannot act after control has gone to a human and back. Automation never takes control back
from a human; resume and abort are explicit operator actions.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

AUTOMATION = "automation"
NOBODY = "none"


def human(operator_id: str) -> str:
    return f"human:{operator_id}"


class LeaseToken(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    holder: str
    generation: int = Field(ge=0)


class ControlLease(BaseModel):
    model_config = ConfigDict(extra="forbid")

    holder: str  # "automation" | "human:<id>" | "none"
    generation: int = Field(ge=0)
    reason: str
    since: datetime

    def token(self) -> LeaseToken:
        return LeaseToken(holder=self.holder, generation=self.generation)
