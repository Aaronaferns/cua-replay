"""Capability artifact schema.

Owner: Agent B (artifact + replay). Schema evolution through M3 belongs to B; other agents request changes through
the orchestrator. Frozen at M3 exit.

This module is a leaf: it imports nothing from the rest of `cua` and contains no Playwright vocabulary.

Seeded by the orchestrator with only the types other packages need for M1/M2: `Effect`, `FieldSpec`,
`Strategy`, `Fingerprint`. Everything else (Target, Step, Contract, Capability, ...) is B's to add.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

# There is deliberately no "write": the planned flows have none, and design-only items never enter enums.
Effect = Literal["none", "read", "commit"]

FieldType = Literal["string", "decimal", "integer", "date"]
Sensitivity = Literal["public", "internal", "confidential", "pii"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class FieldSpec(_Strict):
    """A typed capability input or output."""

    type: FieldType
    description: str = ""
    pattern: str | None = None
    sensitivity: Sensitivity = "internal"


StrategyKind = Literal["role", "same_row", "table_cell", "scoped", "css"]


class Strategy(_Strict):
    """One way to find a target, surface-agnostic. Ranked: role, same_row, table_cell, scoped, css.

    Names are stored as observed and matched normalized-exact (anchored, case-insensitive, whitespace-folded,
    icon glyphs dropped, optional trailing ':'/'*'), never as substrings (spike S7).
    """

    kind: StrategyKind
    role: str | None = None
    name: str | None = None
    # same_row: the row whose label cell equals `label`; `take` = the row's control or its value cell
    label: str | None = None
    take: Literal["control", "value"] | None = None
    # table_cell: the cell in the row keyed by `row_key` under the column headed `column`
    row_key: str | None = None
    column: str | None = None
    # scoped: role+name inside a container identified by role+name (e.g. a column header, form or dialog)
    within_role: str | None = None
    within_name: str | None = None
    # css: structural fallback, ranked last; generated-looking ids are rejected at record time
    css: str | None = None
    why: str
    matched_at_record: str  # e.g. "1 of 1, same element as ref"


class Fingerprint(_Strict):
    """Record-time facts about the target. Any non-primary strategy hit must agree on these hard fields."""

    role: str
    tag: str
    input_type: str | None = None
    frame: str | None = None
    row_label: str | None = None
    column_header: str | None = None
