"""The model seam: what the loop asks of a model, independent of which one.

`ModelAdapter.next_action(obs, feedback)` returns the model's next action (or a protocol problem) plus a usage record.
Adapters keep their own append-only conversation; the loop never edits history. This module imports no model SDK, so
the scripted adapter and the loop run without one.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from cua.agent.actions import ACTION_MODELS, AgentAction
from cua.artifact.schema import FieldSpec
from cua.surface.base import Observation


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Usage(_M):
    """Tokens and the provider request id for one model call (scripted adapters report zeros)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    request_id: str | None = None


class Feedback(_M):
    """What happened to the previous action, for the model: `ok=False` for rejected, denied or failed actions.
    The message never contains input or extracted values."""

    ok: bool
    message: str


TurnStop = Literal["action", "no_action", "invalid_action", "refusal", "error"]


class ModelTurn(_M):
    action: AgentAction | None = None
    stop: TurnStop = "action"
    detail: str | None = None  # why there is no action (refusal category, validation problem, error class)
    usage: Usage = Usage()


class Task(_M):
    """What the run is for. `goal` holds `{{token}}` placeholders, never raw input values."""

    goal: str
    inputs: dict[str, FieldSpec]
    outputs: dict[str, FieldSpec]
    base_url: str


class ModelAdapter(Protocol):
    name: str

    async def start(self, task: Task) -> None: ...

    async def next_action(self, obs: Observation, feedback: Feedback | None = None) -> ModelTurn: ...


# --- the tool vocabulary (shared by every adapter that speaks tools) ---------------------------------------------

_RATIONALE = "One short sentence (plain text, single line) saying why this action moves toward the goal."
_REF = "The [ref=...] of the element, exactly as shown in the current page content."

_PROPERTIES: dict[str, dict[str, dict[str, Any]]] = {
    "click": {"ref": {"type": "string", "description": _REF}},
    "type": {
        "ref": {"type": "string", "description": _REF},
        "text": {"type": "string", "description": "Text to type (replaces the field's content). Use {{name}} placeholders for inputs."},
    },
    "select": {
        "ref": {"type": "string", "description": _REF},
        "option": {"type": "string", "description": "The visible label of the option to choose."},
    },
    "press": {"key": {"type": "string", "description": "One of: Enter, Tab, Shift+Tab, Escape, Backspace, Delete, Space, ArrowUp, ArrowDown, ArrowLeft, ArrowRight, Home, End, PageUp, PageDown."}},
    "navigate": {"url": {"type": "string", "description": "An absolute URL inside the application. Use {{name}} placeholders for inputs."}},
    "extract": {
        "name": {"type": "string", "description": "The name of a declared output."},
        "ref": {"type": "string", "description": _REF},
    },
    "request_approval": {
        "ref": {"type": "string", "description": "The control that would commit a change."},
        "summary": {"type": "string", "description": "What would be committed, in one sentence."},
    },
    "request_help": {"reason": {"type": "string", "description": "What blocks you and what a person would need to do."}},
    "done": {"evidence_refs": {"type": "array", "items": {"type": "string"}, "description": "Refs of the elements on screen that prove the goal is met (for example the element showing the identity you checked and the value you extracted)."}},
}

_DESCRIPTIONS = {
    "click": "Click one element on the current page.",
    "type": "Clear a text field and type into it.",
    "select": "Choose an option of a select box by its visible label.",
    "press": "Press one key.",
    "navigate": "Open a URL inside the application.",
    "extract": "Record the visible text of an element as a declared output.",
    "request_approval": "Ask a person to approve a change before you commit it. Not needed for read-only goals.",
    "request_help": "Stop and ask a person for help when you cannot continue safely.",
    "done": "Declare the goal met. Call it only after every declared output has been extracted.",
}


def tool_definitions() -> list[dict[str, Any]]:
    """The whole action vocabulary as strict tools. Byte-stable: no timestamps, no run data."""
    tools = []
    for kind in ACTION_MODELS:
        props = {"rationale": {"type": "string", "description": _RATIONALE}, **_PROPERTIES[kind]}
        tools.append({
            "name": kind,
            "description": _DESCRIPTIONS[kind],
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": props,
                "required": list(props),
                "additionalProperties": False,
            },
        })
    return tools


def render_observation(obs: Observation) -> str:
    """The observation as the model reads it (text part). Contains only what the page shows."""
    f = obs.facts
    lines = [f"URL: {f.url}", f"Title: {f.title}"]
    if f.native_dialog:
        lines.append(f"A native browser dialog is blocking the page: {f.native_dialog}")
    if f.dialog:
        lines.append(f"Open dialog: {f.dialog}")
    if f.frames_unavailable:
        lines.append("Note: part of the page (a frame) could not be read.")
    if obs.menu_summary:
        lines.append("Navigation menu entries: " + " | ".join(obs.menu_summary))
    lines.append("")
    if obs.dialog_snapshot:
        lines += ["Dialog (act on this first):", obs.dialog_snapshot, "", "Page content behind the dialog:"]
    else:
        lines.append("Page content:")
    lines.append(obs.snapshot or "(empty)")
    return "\n".join(lines)
