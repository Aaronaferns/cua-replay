"""The closed discovery action schema: the model's whole vocabulary.

Owner: Agent A. Seeded by the orchestrator; A owns evolution.

Text arguments may contain input tokens such as `{{customer_code}}`; the executor substitutes the real value at
perform time. A raw input value in any string argument is rejected, as are control characters. Every action carries a
short `rationale` (the stated reason, not hidden reasoning). There is no script execution, upload or download action.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Action(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    rationale: str = Field(max_length=500)


class Click(_Action):
    kind: Literal["click"] = "click"
    ref: str


class Type(_Action):
    kind: Literal["type"] = "type"
    ref: str
    text: str


class Select(_Action):
    kind: Literal["select"] = "select"
    ref: str
    option: str


class Press(_Action):
    kind: Literal["press"] = "press"
    key: str


class Navigate(_Action):
    kind: Literal["navigate"] = "navigate"
    url: str


class Extract(_Action):
    kind: Literal["extract"] = "extract"
    name: str  # a declared output
    ref: str


class RequestApproval(_Action):
    kind: Literal["request_approval"] = "request_approval"
    ref: str  # the commit control the model wants to activate
    summary: str


class RequestHelp(_Action):
    kind: Literal["request_help"] = "request_help"
    reason: str


class Done(_Action):
    kind: Literal["done"] = "done"
    evidence_refs: list[str]  # elements on screen that prove the goal; they become the success checkpoint


AgentAction = Annotated[
    Click | Type | Select | Press | Navigate | Extract | RequestApproval | RequestHelp | Done,
    Field(discriminator="kind"),
]
