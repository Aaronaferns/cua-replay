"""The intervention request: the record that automation needs a human, written into the run bundle.

Owner: Agent C. A request is a small, immutable JSON document (`interventions/<id>.json`, schema
`cua.intervention/v1`) plus an `intervention` evidence event. It says what the run was doing, why it stopped, where the
page was (a URL without query), what the run had already done to the application (`effect_so_far`,
`commit_attempted`), which lease generation the escalation happened under and by when somebody must claim it.

It carries no input or output values: the screenshot is a path to an image the surface masked, the detail text is
value-free, and the model hides inputs in validation errors. Claiming, resuming and aborting are lease transitions
(`cua.control.lease`), not edits of this record; the SLA applies only while the lease holder is `none` (unclaimed), which
is why `overdue` is a pure date check that the caller combines with the lease (or leaves to
`LeaseManager.expire_unclaimed`).

Reason codes are SCREAMING_SNAKE identifiers. `HUMAN_ONLY_LOGIN` (the login form asks for a security code) is the one the
plan names; others are added by their milestone, not here.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cua.artifact.schema import Effect
from cua.control.lease import LeaseToken
from cua.evidence.events import EvidenceEvent, EvidenceWriter
from cua.safety.network import loggable_url

SCHEMA = "cua.intervention/v1"
HUMAN_ONLY_LOGIN = "HUMAN_ONLY_LOGIN"
COMMIT_UNKNOWN = "COMMIT_UNKNOWN"  # M6: a commit was attempted and its outcome is not known; the operator gives a verdict
DEFAULT_SLA_SECONDS = 15 * 60
_REASON = re.compile(r"[A-Z][A-Z0-9_]{2,63}")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


class InterventionRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True, validate_by_name=True, validate_by_alias=True,
        serialize_by_alias=True,
    )

    schema_: Literal["cua.intervention/v1"] = Field(SCHEMA, alias="schema")
    id: str
    run_id: str
    capability: str  # "<app>.<name>"
    version: str | None = None
    step_id: str | None = None
    reason: str  # e.g. HUMAN_ONLY_LOGIN
    detail: str = ""  # safe text: no raw input or output values
    classified_state: str | None = None  # what the classifier made of the screen ("unknown" included)
    expected_state: str | None = None
    url: str  # no query, fragment or userinfo
    screenshot: str | None = None  # bundle-relative path of a masked screenshot
    effect_so_far: Effect = "none"  # the run's effect high-water mark when it stopped
    commit_attempted: bool = False  # a commit was attempted by automation before the stop
    lease: LeaseToken  # the lease the escalation happened under
    created_at: datetime
    sla_seconds: float | None = Field(default=DEFAULT_SLA_SECONDS, gt=0)
    sla_deadline: datetime | None = None

    @field_validator("id", "run_id")
    @classmethod
    def _ident(cls, v: str) -> str:
        if not _ID.fullmatch(v):
            raise ValueError("must be 1-100 characters of letters, digits, '.', '_' or '-'")
        return v

    @field_validator("reason")
    @classmethod
    def _reason(cls, v: str) -> str:
        if not _REASON.fullmatch(v):
            raise ValueError("reason must be an UPPER_SNAKE_CASE code")
        return v

    @field_validator("url")
    @classmethod
    def _no_query(cls, v: str) -> str:
        return loggable_url(v)

    @field_validator("created_at", "sla_deadline")
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return v

    @model_validator(mode="after")
    def _deadline(self) -> InterventionRequest:
        if (self.sla_deadline is None) != (self.sla_seconds is None):
            raise ValueError("sla_seconds and sla_deadline go together")
        if self.sla_deadline is not None and self.sla_deadline < self.created_at:
            raise ValueError("the SLA deadline is before the request was created")
        return self

    @classmethod
    def create(
        cls,
        *,
        id: str,
        run_id: str,
        capability: str,
        reason: str,
        url: str,
        lease: LeaseToken,
        sla_seconds: float | None = DEFAULT_SLA_SECONDS,
        now: datetime | None = None,
        **fields: object,
    ) -> InterventionRequest:
        """Stamp `created_at` (now, UTC) and the SLA deadline (`now + sla_seconds`; `None` = no SLA)."""
        created = now or datetime.now(UTC)
        deadline = None if sla_seconds is None else created + timedelta(seconds=sla_seconds)
        return cls(
            id=id, run_id=run_id, capability=capability, reason=reason, url=url, lease=lease, created_at=created,
            sla_seconds=sla_seconds, sla_deadline=deadline, **fields,  # type: ignore[arg-type]
        )

    def overdue(self, now: datetime | None = None) -> bool:
        """Past the SLA deadline. Only meaningful while the request is unclaimed; the caller checks the lease."""
        return self.sla_deadline is not None and (now or datetime.now(UTC)) > self.sla_deadline


def write_intervention(writer: EvidenceWriter, request: InterventionRequest) -> str:
    """Persist the request through the evidence writer and log the `intervention` event. Returns the bundle-relative
    path (`interventions/<id>.json`)."""
    path = writer.json(f"interventions/{request.id}.json", request.model_dump(mode="json"))
    relpath = path.relative_to(writer.run_dir).as_posix()
    writer.event(
        EvidenceEvent(
            ts=request.created_at, seq=0, run_id=request.run_id, actor="system", kind="intervention",
            step_id=request.step_id, state=request.classified_state, lease=request.lease,
            detail={
                "id": request.id, "reason": request.reason, "request": relpath,
                "sla_deadline": request.sla_deadline.isoformat() if request.sla_deadline else None,
            },
        )
    )
    return relpath
