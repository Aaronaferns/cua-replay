"""The closed discovery action schema: the model's whole vocabulary.

Owner: Agent A. Seeded by the orchestrator; A owns evolution.

Text arguments may contain input tokens such as `{{customer_code}}`; the executor substitutes the real value at
perform time. A raw input value in any string argument is rejected, as are control characters. Every action carries a
short `rationale` (the stated reason, not hidden reasoning). There is no script execution, upload or download action.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


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


ACTION_MODELS: dict[str, type[_Action]] = {
    m.model_fields["kind"].default: m  # type: ignore[misc]
    for m in (Click, Type, Select, Press, Navigate, Extract, RequestApproval, RequestHelp, Done)
}
"""Every action kind the model may choose, in the order its tools are declared."""

_ADAPTER: TypeAdapter[AgentAction] = TypeAdapter(AgentAction)


def parse_action(kind: str, arguments: dict[str, object]) -> AgentAction:
    """Validate a model-chosen tool call. Raises `pydantic.ValidationError` (which hides input values)."""
    return _ADAPTER.validate_python({**arguments, "kind": kind})


def string_arguments(action: AgentAction) -> list[tuple[str, str]]:
    """Every string the action carries, as (field, text): the surface the input-value and control-character checks
    must cover. Lists contribute each element."""
    out: list[tuple[str, str]] = []
    for field in type(action).model_fields:
        if field == "kind":
            continue
        value = getattr(action, field)
        if isinstance(value, str):
            out.append((field, value))
        elif isinstance(value, list):
            out.extend((field, v) for v in value if isinstance(v, str))
    return out
