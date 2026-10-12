"""Target resolution: ranked strategies, exactly one match, fingerprint-gated fallbacks.

Rules (all from the plan, none negotiable):

* A strategy that matches zero elements or several has failed. There is no "first of many".
* The primary strategy (`strategies[0]`) is trusted on its own. Any other strategy's single hit must agree with the
  target's record-time fingerprint (role, tag, input type, frame, row label, column header) or it is rejected: a
  fallback that finds the wrong control is worse than no fallback.
* Using a fallback is never silent: the caller gets a `DriftWarning`.
* Output extracts resolve with the primary strategy only (`primary_only`), so a drifted page can never make a number
  come from somewhere else.

`${input}` references are substituted into the strategy before it is handed to the surface; descriptions used in
messages are always the unsubstituted form, so no input value reaches a result.

Imports: artifact, surface, replay.result only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cua.artifact.names import names_match
from cua.artifact.schema import LOCATOR_FIELDS, TEMPLATE_RE, Fingerprint, Strategy, Target
from cua.replay.result import DriftWarning
from cua.surface.base import Element, Surface


class ResolutionError(Exception):
    """No acceptable element. `code` is a failure code; `detail` is safe text (strategy descriptions, counts)."""

    def __init__(self, code: str, detail: str, failed: list[str]):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.failed = failed


@dataclass(frozen=True)
class Resolved:
    element: Element
    strategy: Strategy  # the unsubstituted strategy that produced the element
    index: int
    failed: list[str] = field(default_factory=list)  # descriptions of the strategies tried before this one

    def drift(self, step_id: str, target: str) -> DriftWarning | None:
        if self.index == 0:
            return None
        return DriftWarning(step_id=step_id, target=target, failed_strategies=self.failed,
                            used_strategy=self.strategy.describe())


def substitute(strategy: Strategy, inputs: dict[str, str]) -> Strategy:
    """Replace `${name}` in every locator string with the input's value."""
    update = {}
    for f in LOCATOR_FIELDS:
        value = getattr(strategy, f)
        if isinstance(value, str) and "${" in value:
            update[f] = TEMPLATE_RE.sub(lambda m: inputs[m.group(1)], value)
    return strategy.model_copy(update=update) if update else strategy


def fingerprint_mismatches(recorded: Fingerprint, observed: Fingerprint) -> list[str]:
    """Hard fields on which the observed element disagrees with the record. Names compare normalized-exact."""
    bad = []
    if recorded.role != observed.role:
        bad.append("role")
    if recorded.tag != observed.tag:
        bad.append("tag")
    if recorded.input_type is not None and recorded.input_type != observed.input_type:
        bad.append("input_type")
    if recorded.frame is not None and recorded.frame != observed.frame:
        bad.append("frame")
    for name in ("row_label", "column_header"):
        want = getattr(recorded, name)
        if want is not None and not names_match(want, getattr(observed, name)):
            bad.append(name)
    return bad


async def resolve(
    surface: Surface, target: Target, inputs: dict[str, str], *, primary_only: bool = False
) -> Resolved:
    strategies = target.strategies[:1] if primary_only else target.strategies
    failed: list[str] = []
    saw_many = saw_rejected = False
    for index, strategy in enumerate(strategies):
        label = strategy.describe()
        elements = await surface.find(substitute(strategy, inputs))
        if len(elements) == 1:
            if index == 0:
                return Resolved(elements[0], strategy, 0, failed)
            observed = await surface.fingerprint(elements[0])
            if not (bad := fingerprint_mismatches(target.fingerprint, observed)):
                return Resolved(elements[0], strategy, index, failed)
            saw_rejected = True
            failed.append(f"{label} [fingerprint differs: {', '.join(bad)}]")
        elif len(elements) == 0:
            failed.append(f"{label} [no match]")
        else:
            saw_many = True
            failed.append(f"{label} [{len(elements)} matches]")
    scope = "the primary strategy" if primary_only else f"{len(strategies)} strateg{'y' if len(strategies) == 1 else 'ies'}"
    detail = f"{scope} failed: " + "; ".join(failed)
    if saw_rejected:
        raise ResolutionError("TARGET_FINGERPRINT_MISMATCH", detail, failed)
    if saw_many:
        raise ResolutionError("TARGET_AMBIGUOUS", detail, failed)
    raise ResolutionError("TARGET_NOT_FOUND", detail, failed)
