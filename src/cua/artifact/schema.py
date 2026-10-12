"""Capability artifact and app-profile schema.

Owner: Agent B (artifact + replay). Frozen at M3 exit; other agents request changes through the orchestrator.

A capability is a *guarded linear program over classified screen states*: a linear list of steps, each of which
starts only in its expected state, acts on one target found by ranked strategies (exactly one match, never the first
of many), may verify a postcondition, and may branch (`on`) only where the screen can legitimately differ. States
are defined once per app in the trusted profile, not in the capability.

Two files are hashed together into the lock (see `cua.artifact.lock`):

    capability  contract (typed inputs, outputs, business outcomes), steps, targets, success checkpoint, provenance.
                No status, approval or hash inside: those live in the approval sidecar.
    profile     screen catalog (states with content signatures, `specializes`), default meanings, entry route,
                login form, commit routes, number format, handlers.

The language is closed: actions and predicates are discriminated unions with `extra="forbid"`; there is no field
that can carry code. This module is a leaf (pydantic + stdlib) and contains no browser-engine vocabulary.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cua.artifact.predicates import Predicate

# There is deliberately no "write": the planned flows have none, and design-only items never enter enums.
Effect = Literal["none", "read", "commit"]
EFFECT_RANK: dict[str, int] = {"none": 0, "read": 1, "commit": 2}

FieldType = Literal["string", "decimal", "integer", "date"]
Sensitivity = Literal["public", "internal", "confidential", "pii"]

NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")  # input, output, outcome, state, target and step ids
TEMPLATE_RE = re.compile(r"\$\{([a-z][a-z0-9_]*)\}")  # ${input_name} inside action text, routes and strategies


def max_effect(effects: list[str]) -> Effect:
    return max(effects, key=EFFECT_RANK.__getitem__, default="none")  # type: ignore[return-value]


def template_refs(s: str | None) -> list[str]:
    """Input names referenced as ${name} in a string."""
    return TEMPLATE_RE.findall(s) if s else []


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


def _check_name(kind: str, value: str) -> str:
    if not NAME_RE.fullmatch(value):
        raise ValueError(f"{kind} id must match {NAME_RE.pattern}")
    return value


class FieldSpec(_Strict):
    """A typed capability input or output.

    Values cross the boundary as strings: decimals are exact decimal strings ("75.37"), dates ISO 8601. `pattern`
    is a full-match regex for inputs and string outputs.
    """

    type: FieldType
    description: str = ""
    pattern: str | None = None
    sensitivity: Sensitivity = "internal"

    @field_validator("pattern")
    @classmethod
    def _pattern_compiles(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                re.compile(v)
            except re.error:
                raise ValueError("pattern is not a valid regular expression") from None
        return v


# --- targets ---------------------------------------------------------------------------------------------------

StrategyKind = Literal["role", "same_row", "table_cell", "scoped", "css"]

# Which locator fields each strategy kind uses: (required, optional). Anything else set on a strategy is an error.
_STRATEGY_FIELDS: dict[str, tuple[set[str], set[str]]] = {
    "role": ({"role", "name"}, set()),
    "same_row": ({"label", "take"}, {"role"}),
    "table_cell": ({"column"}, {"role", "row_key"}),
    "scoped": ({"role", "within_role", "within_name"}, {"name"}),
    "css": ({"css"}, set()),
}
LOCATOR_FIELDS = ("role", "name", "label", "take", "row_key", "column", "within_role", "within_name", "css")


class Strategy(_Strict):
    """One way to find a target, surface-agnostic. Ranked: role, same_row, table_cell, scoped, css.

    Names are stored as observed and matched normalized-exact (anchored, case-insensitive, whitespace-folded,
    icon glyphs dropped, optional trailing ':'/'*'), never as substrings (spike S7). String fields may reference an
    input as `${name}`; the resolver substitutes the value before searching.
    """

    kind: StrategyKind
    role: str | None = None
    name: str | None = None
    # same_row: the row whose label cell equals `label`; `take` = the row's control or its value cell
    label: str | None = None
    take: Literal["control", "value"] | None = None
    # table_cell: the cell (or the `role` element inside it) in the column headed `column`, in the row keyed by
    # `row_key` when given (otherwise the table must have exactly one such cell)
    row_key: str | None = None
    column: str | None = None
    # scoped: role (+ name) inside a container identified by role + name (a column header, form or dialog)
    within_role: str | None = None
    within_name: str | None = None
    # css: structural fallback, ranked last; generated-looking ids are rejected at record time
    css: str | None = None
    why: str
    matched_at_record: str  # e.g. "1 of 1, same element as ref"

    @model_validator(mode="after")
    def _fields_fit_kind(self) -> Strategy:
        required, optional = _STRATEGY_FIELDS[self.kind]
        present = {f for f in LOCATOR_FIELDS if getattr(self, f) is not None}
        if missing := required - present:
            raise ValueError(f"{self.kind} strategy needs {sorted(missing)}")
        if extra := present - required - optional:
            raise ValueError(f"{self.kind} strategy does not use {sorted(extra)}")
        return self

    def locator_strings(self) -> list[str]:
        """The strings a surface searches with (never `why` / `matched_at_record`)."""
        return [v for f in LOCATOR_FIELDS if isinstance(v := getattr(self, f), str)]

    def describe(self) -> str:
        """Short human description, used in drift warnings, failures and the review card."""
        if self.kind == "role":
            return f'role {self.role} "{self.name}"'
        if self.kind == "same_row":
            return f'{self.take} in the row labelled "{self.label}"'
        if self.kind == "table_cell":
            what = self.role or "cell"
            row = f' of the row keyed "{self.row_key}"' if self.row_key else " of the only row"
            return f'{what} in column "{self.column}"{row}'
        if self.kind == "scoped":
            name = f' "{self.name}"' if self.name else ""
            return f'{self.role}{name} within {self.within_role} "{self.within_name}"'
        return f"css {self.css}"


class Fingerprint(_Strict):
    """Record-time facts about the target. Any non-primary strategy hit must agree on these hard fields."""

    role: str
    tag: str
    input_type: str | None = None
    frame: str | None = None
    row_label: str | None = None
    column_header: str | None = None


class Target(_Strict):
    """An element a step acts on or reads. `strategies[0]` is primary; the rest are ranked fallbacks."""

    description: str
    strategies: list[Strategy] = Field(min_length=1)
    fingerprint: Fingerprint


# --- actions ---------------------------------------------------------------------------------------------------


class NavigateAction(_Strict):
    kind: Literal["navigate"] = "navigate"
    route: str  # path + query relative to the app origin; ${input} values are URL-encoded

    @field_validator("route")
    @classmethod
    def _relative(cls, v: str) -> str:
        if not v.startswith("/") or v.startswith("//"):
            raise ValueError("route must be a path relative to the app origin")
        return v


class ClickAction(_Strict):
    kind: Literal["click"] = "click"
    target: str


class TypeAction(_Strict):
    kind: Literal["type"] = "type"
    target: str
    text: str  # literal text and ${input} references; the field is cleared first


class SelectAction(_Strict):
    kind: Literal["select"] = "select"
    target: str
    option: str  # option label, ${input} allowed


class PressAction(_Strict):
    kind: Literal["press"] = "press"
    key: Literal["Enter", "Tab", "Escape"]


class ExtractAction(_Strict):
    kind: Literal["extract"] = "extract"
    target: str
    output: str  # a declared output; the text is parsed to the output's type


class AssertAction(_Strict):
    """A first-class checkpoint step: the predicate must hold on the current screen, else the run stops."""

    kind: Literal["assert"] = "assert"
    that: Predicate
    on_fail: Literal["IDENTITY_MISMATCH", "CHECKPOINT_FAILED"] = "CHECKPOINT_FAILED"


Action = Annotated[
    NavigateAction | ClickAction | TypeAction | SelectAction | PressAction | ExtractAction | AssertAction,
    Field(discriminator="kind"),
]


def action_target(action: Action) -> str | None:
    return getattr(action, "target", None)


# --- steps, contract, capability -------------------------------------------------------------------------------


class Branch(_Strict):
    """Where a step may lead: continue with the next step, or end with a declared business outcome."""

    state: str
    then: Literal["next"] | None = None
    outcome: str | None = None

    @model_validator(mode="after")
    def _one(self) -> Branch:
        if (self.then is None) == (self.outcome is None):
            raise ValueError("a branch needs exactly one of 'then' or 'outcome'")
        return self


class Step(_Strict):
    id: str
    intent: str  # one line a reviewer reads
    expect: str  # state the step must start in
    do: Action
    effect: Effect
    navigates: bool = False  # the action loads a new document: wait for the navigation fence before classifying
    on: list[Branch] = []  # explicit transitions; empty = the next step's `expect` (or success.state)
    verify: Predicate | None = None  # postcondition right after the action (e.g. value echo)
    timeout_ms: int = Field(10_000, ge=500, le=120_000)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        return _check_name("step", v)


class Outcome(_Strict):
    """A declared business outcome: a legitimate answer for the caller, not a failure."""

    description: str
    returns: list[str] = []  # declared outputs returned with this outcome


class Contract(_Strict):
    inputs: dict[str, FieldSpec]
    outputs: dict[str, FieldSpec]
    outcomes: dict[str, Outcome] = {}

    @model_validator(mode="after")
    def _names(self) -> Contract:
        for kind, names in (("input", self.inputs), ("output", self.outputs), ("outcome", self.outcomes)):
            for name in names:
                _check_name(kind, name)
        return self


class Success(_Strict):
    state: str  # the run must end in this state (or a specialization of it)
    outputs: list[str]  # every declared output, extracted on the main path
    checks: list[Predicate] = []


class Provenance(_Strict):
    """Where the capability came from. Hashed with the body: re-recording is a change that needs re-approval."""

    trace_run_id: str
    trace_sha256: str
    discovered_by: str
    compiler: str


SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
CAPABILITY_ID_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")

_DOCUMENT = ConfigDict(
    extra="forbid", frozen=True, hide_input_in_errors=True,
    validate_by_name=True, validate_by_alias=True, serialize_by_alias=True,
)


class Capability(_Strict):
    model_config = _DOCUMENT

    schema_: Literal["cua.capability/v1"] = Field("cua.capability/v1", alias="schema")
    id: str  # "<app>.<name>", e.g. "dolibarr.lookup_customer_outstanding"
    version: str  # semver: MAJOR = contract change, MINOR = steps or states, PATCH = strategy-only edits
    title: str
    description: str
    app: str  # must equal the profile's app
    contract: Contract
    effect: Effect  # ceiling: the maximum effect of any step; replay never exceeds it
    steps: list[Step] = Field(min_length=1)
    success: Success
    targets: dict[str, Target]
    provenance: Provenance

    @field_validator("app")
    @classmethod
    def _app(cls, v: str) -> str:
        return _check_name("app", v)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not CAPABILITY_ID_RE.fullmatch(v):
            raise ValueError(f"capability id must match {CAPABILITY_ID_RE.pattern}")
        return v

    @field_validator("version")
    @classmethod
    def _semver(cls, v: str) -> str:
        if not SEMVER_RE.fullmatch(v):
            raise ValueError("version must be MAJOR.MINOR.PATCH")
        return v

    @property
    def name(self) -> str:
        return self.id.split(".", 1)[1]


# --- app profile -----------------------------------------------------------------------------------------------


class State(_Strict):
    """A classified screen. The signature reads screen content (static predicates only): the login form appears at
    any URL after a session is lost, so URLs alone never identify a state.

    A state that `specializes` another is that state plus its own signature (child wins when both match); any other
    overlap between matching states is ambiguous and fails closed.
    """

    description: str
    layer: Literal["page", "dialog"] = "page"  # dialog states are matched against the dialog layer only
    signature: Predicate
    specializes: str | None = None
    origin: Literal["observed", "declared"]  # observed = template-proposed from a recorded screen
    edited: bool = False  # a human changed an observed proposal


class Meaning(_Strict):
    """What a state means when it appears and the step did not branch on it."""

    outcome: str | None = None  # a business outcome (applies when the capability declares it)
    failure: str | None = None  # a failure code

    @model_validator(mode="after")
    def _one(self) -> Meaning:
        if (self.outcome is None) == (self.failure is None):
            raise ValueError("a meaning needs exactly one of 'outcome' or 'failure'")
        if self.failure is not None and not re.fullmatch(r"[A-Z][A-Z0-9_]+", self.failure):
            raise ValueError("failure codes are UPPER_SNAKE")
        return self


class Handler(_Strict):
    """A budgeted recovery for a content-positive state. Effect none only: it runs the login flow or clicks one
    dismiss control, then re-classifies or restarts from the entry (only while retry-safe)."""

    state: str
    flow: Literal["login"] | None = None
    dismiss: Target | None = None
    then: Literal["reclassify", "restart_if_retry_safe"]
    max_per_run: int = Field(1, ge=1, le=3)

    @model_validator(mode="after")
    def _one(self) -> Handler:
        if (self.flow is None) == (self.dismiss is None):
            raise ValueError("a handler needs exactly one of 'flow' or 'dismiss'")
        return self


class Entry(_Strict):
    route: str  # path relative to the app origin
    start_state: str


class LoginForm(_Strict):
    """The scripted login: credentials come from the run's settings, never from an artifact or a model."""

    state: str  # the state in which the form is filled
    username: Target
    password: Target
    submit: Target


class RoutePattern(_Strict):
    """A URL that commits: path glob plus required query parameters (each a glob). Dolibarr commits are GET
    requests with a CSRF token, so HTTP method cannot identify them."""

    path: str
    query: dict[str, str] = {}


class NumberFormat(_Strict):
    decimal: Literal[".", ","] = "."
    group: Literal[",", ".", " ", "'", ""] = ","

    @model_validator(mode="after")
    def _distinct(self) -> NumberFormat:
        if self.decimal == self.group:
            raise ValueError("decimal and group separators must differ")
        return self


class Profile(_Strict):
    model_config = _DOCUMENT

    schema_: Literal["cua.profile/v1"] = Field("cua.profile/v1", alias="schema")
    app: str  # e.g. "dolibarr"
    entry: Entry
    login: LoginForm
    states: dict[str, State]
    default_meaning: dict[str, Meaning] = {}
    handlers: dict[str, Handler] = {}
    commit_routes: list[RoutePattern] = []
    number_format: NumberFormat = NumberFormat()

    @model_validator(mode="after")
    def _ids(self) -> Profile:
        _check_name("app", self.app)
        for sid in self.states:
            _check_name("state", sid)
        return self

    def ancestors(self, state: str) -> list[str]:
        """The state's parents, nearest first (cycles are rejected by `validate_profile`)."""
        chain: list[str] = []
        parent = self.states[state].specializes if state in self.states else None
        while parent is not None and parent not in chain and parent in self.states:
            chain.append(parent)
            parent = self.states[parent].specializes
        return chain

    def lineage(self, state: str) -> list[str]:
        """The state itself, then its ancestors: the order in which meanings are looked up."""
        return [state, *self.ancestors(state)]

    def is_a(self, state: str, other: str) -> bool:
        return state == other or other in self.ancestors(state)

    def specializations(self, state: str) -> list[str]:
        return sorted(s for s in self.states if state in self.ancestors(s))
