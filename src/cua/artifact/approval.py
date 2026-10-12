"""Approval sidecar: change control for a capability, bound to the lock hash.

Approval is four eyes on the *procedure* (what replay will do), not authorization of a transaction. It lives beside
the capability (`approvals/<id>@<version>.json`), never inside the hashed body. It binds to the lock hash over
capability + profile, so any behavior change (a step, a strategy, a state signature, a meaning, a commit route)
makes it stale. The approver must not be the person who ran the discovery.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cua.artifact.lock import lock_hash
from cua.artifact.names import normalize_name
from cua.artifact.schema import Capability, Profile


class SelfApprovalError(ValueError):
    """The approver is the discoverer."""


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    capability: str
    version: str
    lock_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    approver: str = Field(min_length=1)
    at: datetime
    validation_run_ids: list[str] = Field(min_length=1)


ApprovalState = Literal["approved", "stale", "unapproved"]


def _same_person(a: str, b: str) -> bool:
    return normalize_name(a) == normalize_name(b)


def approve(
    capability: Capability, profile: Profile, *, approver: str, at: datetime, validation_run_ids: list[str]
) -> Approval:
    if _same_person(approver, capability.provenance.discovered_by):
        raise SelfApprovalError("the approver ran the discovery; a different person must approve")
    return Approval(
        capability=capability.id,
        version=capability.version,
        lock_hash=lock_hash(capability, profile),
        approver=approver,
        at=at,
        validation_run_ids=validation_run_ids,
    )


def approval_state(capability: Capability, profile: Profile, approval: Approval | None) -> ApprovalState:
    """`approved` only if the sidecar names this capability and version and its lock hash matches what would run."""
    if approval is None:
        return "unapproved"
    if (
        approval.capability != capability.id
        or approval.version != capability.version
        or approval.lock_hash != lock_hash(capability, profile)
        or _same_person(approval.approver, capability.provenance.discovered_by)
    ):
        return "stale"
    return "approved"


def sidecar_path(approvals_dir: Path, capability: Capability) -> Path:
    return approvals_dir / f"{capability.id}@{capability.version}.json"


def load_approval(path: Path) -> Approval | None:
    return Approval.model_validate_json(path.read_bytes()) if path.exists() else None
