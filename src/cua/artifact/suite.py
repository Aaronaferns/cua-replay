"""The capability test suite (`<capability>.tests.json`): cases a validation run must reproduce.

A case names the inputs, the status and outcome replay must end in, optionally the exact outputs, optionally a
*ground truth* (a named oracle the validation command consults, for example the application's own REST view of a
customer's outstanding amount), and the recoveries a run is expected to need. Anything not listed is expected not to
happen: no recoveries, no drift.

The suite is data for the validation command only. Replay never reads it and never consults an oracle.

Leaf module: pydantic and this package.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cua.artifact.schema import NAME_RE, Capability

Oracle = Literal["dolibarr.customer_outstanding"]  # closed: a new oracle is a code change, reviewed like one
Status = Literal["success", "business_outcome", "failure", "indeterminate"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class GroundTruth(_Strict):
    """The oracle's answer for the value of input `input` must equal the output `output`."""

    output: str
    oracle: Oracle
    input: str


class Expect(_Strict):
    status: Status
    outcome: str  # "success", a declared business outcome, or a failure code
    outputs: dict[str, str] = {}  # exact expected output values (decimal strings, ISO dates)
    ground_truth: list[GroundTruth] = []
    recoveries: list[str] = []  # handler names, as a multiset; the default is "none needed"
    allow_drift: bool = False  # a fallback strategy may be used without failing the case


class Case(_Strict):
    id: str
    description: str = ""
    inputs: dict[str, str]
    expect: Expect
    reset: bool = True  # restore the target to its baseline before this case
    # How the suite runner disturbs the run; replay knows none of this (the runner attaches it from outside):
    faults: list[dict[str, Any]] = []  # fault specs (`kind`, `match`, ...), installed on the browser context
    run_as: Literal["noperm"] | None = None  # log in as the user with no rights instead of the run user
    expire_session_before: str | None = None  # step id: the session is deleted server-side before that step

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not NAME_RE.fullmatch(v):
            raise ValueError(f"case id must match {NAME_RE.pattern}")
        return v


class Suite(_Strict):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True, validate_by_name=True,
                              validate_by_alias=True, serialize_by_alias=True)

    schema_: Literal["cua.suite/v1"] = Field("cua.suite/v1", alias="schema")
    capability: str  # "<app>.<name>"
    cases: list[Case] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> Suite:
        ids = [c.id for c in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("case ids must be unique")
        return self


def suite_problems(suite: Suite, capability: Capability) -> list[str]:
    """References from the suite to the capability's contract (names only, never values)."""
    problems: list[str] = []
    if suite.capability != capability.id:
        problems.append(f"the suite is for {suite.capability!r}, not {capability.id!r}")
    inputs, outputs = capability.contract.inputs, capability.contract.outputs
    outcomes = set(capability.contract.outcomes)
    for case in suite.cases:
        for name in case.inputs:
            if name not in inputs:
                problems.append(f"case {case.id}: unknown input {name!r}")
        for name in case.expect.outputs:
            if name not in outputs:
                problems.append(f"case {case.id}: unknown output {name!r}")
        for truth in case.expect.ground_truth:
            if truth.output not in outputs:
                problems.append(f"case {case.id}: ground truth for unknown output {truth.output!r}")
            if truth.input not in inputs:
                problems.append(f"case {case.id}: ground truth from unknown input {truth.input!r}")
            elif truth.input not in case.inputs:
                problems.append(f"case {case.id}: ground truth input {truth.input!r} is not given")
        if case.expire_session_before is not None and case.expire_session_before not in {s.id for s in capability.steps}:
            problems.append(f"case {case.id}: no step {case.expire_session_before!r} to expire the session before")
        e = case.expect
        if e.status == "success" and e.outcome != "success":
            problems.append(f"case {case.id}: a success has the outcome 'success'")
        if e.status == "business_outcome" and e.outcome not in outcomes:
            problems.append(f"case {case.id}: {e.outcome!r} is not a declared outcome")
    return problems
