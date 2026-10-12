"""Records a discovery run as a `RecordedTrace`: the handoff to the compiler.

The recorder sees raw values (it must, to find where an input is displayed); the trace it builds holds structure
instead: tokens in actions, `TokenSighting`s that point at the element showing an input's value, locator candidates
that already passed the 1-of-1 + same-element filter. The trace is persisted only through the evidence writer, which
redacts it; the compiler works from that persisted copy.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Literal

from cua.agent.actions import AgentAction
from cua.artifact.names import normalize_name
from cua.artifact.schema import Effect, FieldSpec
from cua.compiler.locators import record_candidates
from cua.compiler.trace import RecordedAction, RecordedTrace, ScreenRecord, TargetRecord, TokenSighting
from cua.safety.policy import PolicyDecision
from cua.surface import snapshot as snap
from cua.surface.base import Element, Observation, Surface, SurfaceError
from cua.surface.names import clean

# Controls whose displayed text is the value somebody typed, not a value the application shows.
_INPUT_ROLES = {"textbox", "combobox", "searchbox", "spinbutton", "listbox", "checkbox", "radio", "slider", "switch"}


def actionable_text(obs: Observation) -> str:
    """The snapshot that carries the live refs: the dialog when one is open, else the main content."""
    return obs.dialog_snapshot or obs.snapshot


class Recorder:
    def __init__(
        self,
        surface: Surface,
        *,
        run_id: str,
        app: str,
        goal: str,
        model: str,
        inputs: Mapping[str, FieldSpec],
        outputs: Mapping[str, FieldSpec],
        input_values: Mapping[str, str],
        entry_url: str,
        started_at: datetime | None = None,
    ) -> None:
        self.surface = surface
        self.run_id, self.app, self.goal, self.model = run_id, app, goal, model
        self.inputs, self.outputs = dict(inputs), dict(outputs)
        self._values = dict(input_values)
        self.entry_url = entry_url
        self.started_at = started_at or datetime.now(UTC)
        self.actions: list[RecordedAction] = []
        self._sighted: set[str] = set()

    # -- screens ---------------------------------------------------------------------------------------------------

    @staticmethod
    def screen(obs: Observation) -> ScreenRecord:
        """What the compiler classifies: the scoped outline, dialog layer included (the same text `parse_outline`
        reads). The persisted copy is redacted by the evidence writer."""
        text = obs.snapshot if not obs.dialog_snapshot else f"{obs.snapshot}\n{obs.dialog_snapshot}"
        return ScreenRecord(
            epoch=obs.epoch, url=obs.facts.url, title=obs.facts.title, heading=snap.first_heading(obs.snapshot),
            dialog=obs.facts.dialog, snapshot=text,
        )

    # -- targets ---------------------------------------------------------------------------------------------------

    async def target(self, element: Element, *, ref: str, role: str, name: str | None, dialog: str | None) -> TargetRecord:
        """Capture an element while its ref is live. Runs `find()`, which invalidates the observation's refs."""
        fingerprint = await self.surface.fingerprint(element)
        shown = clean(name) if name else ""
        candidates = await record_candidates(
            self.surface, element, role=role, name=shown or None, fingerprint=fingerprint, inputs=self._values,
            dialog=dialog,
        )
        return TargetRecord(ref=ref, role=role, name=_mask_values(shown, self._values), fingerprint=fingerprint,
                            candidates=candidates)

    # -- token sightings -------------------------------------------------------------------------------------------

    def sighting_refs(self, obs: Observation) -> list[tuple[str, snap.Node]]:
        """(token, node) pairs: display elements in this observation whose text is an input's value. One per token
        that has not been sighted yet. Pure text work: no `find()`, so the refs stay live."""
        found: list[tuple[str, snap.Node]] = []
        nodes = snap.parse(actionable_text(obs))
        for token, value in self._values.items():
            if token in self._sighted or not value:
                continue
            wanted = normalize_name(value)
            for i, node in enumerate(nodes):
                if not node.ref or node.role in _INPUT_ROLES or node.role in ("link", "button", "heading"):
                    continue
                if wanted and wanted in (normalize_name(node.name or ""), normalize_name(node.value or "")):
                    found.append((token, value_cell(nodes, i)))
                    break
        return found

    async def sightings(self, obs: Observation, wanted: list[tuple[str, snap.Node]]) -> list[TokenSighting]:
        """Resolve the sighted elements and keep those a later replay can find by the row's label (rank 2): a label
        and its value is what an identity check reads. Resolves every ref first, then records (find() drops refs)."""
        resolved: list[tuple[str, snap.Node, Element]] = []
        for token, node in wanted:
            try:
                resolved.append((token, node, await self.surface.element_for_ref(node.ref or "", obs.epoch)))
            except SurfaceError:
                continue
        out: list[TokenSighting] = []
        for token, node, element in resolved:
            rec = await self.target(element, ref=node.ref or "", role=node.role, name=node.name or node.value,
                                    dialog=obs.facts.dialog)
            if any(c.rank == 2 for c in rec.candidates):
                out.append(TokenSighting(token=token, target=rec))
                self._sighted.add(token)
        return out

    # -- the trace -------------------------------------------------------------------------------------------------

    def add(
        self,
        action: AgentAction,
        *,
        tokens: list[str],
        effect: Effect,
        policy: PolicyDecision,
        status: Literal["performed", "blocked", "error"],
        error: str | None,
        target: TargetRecord | None,
        pre: ScreenRecord,
        post: ScreenRecord | None,
        sightings: list[TokenSighting],
        started_at: datetime,
        duration_ms: int,
    ) -> RecordedAction:
        recorded = RecordedAction(
            seq=len(self.actions) + 1, action=action, tokens=tokens, effect=effect, policy=policy, status=status,
            error=error, target=target, pre=pre, post=post, sightings=sightings, started_at=started_at,
            duration_ms=duration_ms,
        )
        self.actions.append(recorded)
        return recorded

    def finish(self, outcome: Literal["done", "stopped"], stop_reason: str | None,
               done_evidence: list[TargetRecord]) -> RecordedTrace:
        return RecordedTrace(
            run_id=self.run_id, app=self.app, goal=self.goal, model=self.model, started_at=self.started_at,
            inputs=self.inputs, outputs=self.outputs, entry_url=self.entry_url, actions=self.actions,
            outcome=outcome, stop_reason=stop_reason, done_evidence=done_evidence,
        )


def value_cell(nodes: list[snap.Node], i: int) -> snap.Node:
    """A text-only value cell shows up as a `cell` holding one `generic: text`; the element a label's row leads to
    is the cell, so that is what gets recorded."""
    node = nodes[i]
    for parent in reversed(nodes[:i]):
        if parent.depth < node.depth:
            if parent.role in ("cell", "gridcell") and parent.ref and not parent.name:
                return parent
            break
    return node


def _mask_values(text: str, values: Mapping[str, str]) -> str:
    for token, value in sorted(values.items(), key=lambda kv: -len(kv[1])):
        if value:
            text = text.replace(value, "{{" + token + "}}")
    return text
