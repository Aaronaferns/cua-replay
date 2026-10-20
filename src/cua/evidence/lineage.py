"""Which compiled step came from which discovery turn: a discovery bundle's `trace.json` against its
`capability.draft.json`.

The compiler records no per-step source (adding one would change the artifact and its lock hash), but its output is
regular enough to read back, and this module mirrors exactly those rules (`cua.compiler.compile._compile`; a test pins
the two together, since evidence may not import the compiler):

- a performed action of a kept kind becomes one step, in order, whose `intent` is the action's rationale with
  `{{name}}` written `<name>`;
- "The review screen shows the requested <x>" asserts are inserted just before a commit step: they belong to the
  turn that commits;
- "The screen shows the requested <x>" asserts follow the action after which the value was sighted: they belong to
  that turn;
- everything else in the trace is not a step: failed or blocked attempts are dropped, and `done` becomes the success
  checkpoint (its evidence refs become the checkpoint's checks).

A draft edited by hand, or compiled by an older compiler, may not line up; a step that does not is reported as
unmatched rather than guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

KEPT = ("navigate", "click", "type", "select", "press", "extract")
_TOKEN = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")
_REVIEW = "The review screen shows the requested <"
_SIGHTED = "The screen shows the requested <"


def intent_of(rationale: str) -> str:
    """The compiler's step intent for an action rationale (`cua.compiler.compile._intent`)."""
    return " ".join(_TOKEN.sub(r"<\1>", rationale).split()) or "step"


@dataclass
class Link:
    step: dict[str, Any]
    role: str  # "compiled" | "check_before" (identity check before a commit) | "check_after" (value sighted)


@dataclass
class Lineage:
    capability: dict[str, Any]
    by_turn: dict[int, list[Link]] = field(default_factory=dict)
    dropped: dict[int, str] = field(default_factory=dict)  # turn -> why it is not a step
    unmatched: list[dict[str, Any]] = field(default_factory=list)
    effect_changes: dict[str, tuple[str, str]] = field(default_factory=dict)  # step id -> (recorded, compiled)

    @property
    def steps(self) -> list[dict[str, Any]]:
        return list(self.capability.get("steps") or [])

    @property
    def targets(self) -> dict[str, Any]:
        return dict(self.capability.get("targets") or {})

    @property
    def success(self) -> dict[str, Any]:
        return dict(self.capability.get("success") or {})

    def step_ids(self, turn: int) -> list[str]:
        return [link.step["id"] for link in self.by_turn.get(turn, [])]

    def counts(self) -> dict[str, int]:
        links = [link for links in self.by_turn.values() for link in links]
        return {
            "turns": len({*self.by_turn, *self.dropped}),
            "steps": len(self.steps),
            "compiled": sum(1 for x in links if x.role == "compiled"),
            "checks": sum(1 for x in links if x.role != "compiled"),
            "dropped": sum(1 for why in self.dropped.values() if not why.startswith("done")),
            "unmatched": len(self.unmatched),
        }


def _seq(action: dict[str, Any]) -> int | None:
    try:
        return int(action.get("seq"))
    except (TypeError, ValueError):
        return None


def _why_dropped(a: dict[str, Any]) -> str:
    kind = (a.get("action") or {}).get("kind")
    if kind == "done":
        return "done: becomes the success checkpoint"
    if a.get("status") != "performed":
        detail = a.get("error") or a.get("status")
        return f"not a step: the attempt did not succeed ({detail})"
    return f"not a step: '{kind}' is not part of the artifact language"


def build(trace: dict[str, Any] | None, capability: dict[str, Any] | None) -> Lineage | None:
    if not trace or not capability or not capability.get("steps"):
        return None
    lineage = Lineage(capability=capability)
    actions = [a for a in trace.get("actions", []) if _seq(a) is not None]
    kept = [a for a in actions if a.get("status") == "performed" and (a.get("action") or {}).get("kind") in KEPT]
    kept_seqs = {_seq(a) for a in kept}
    for a in actions:
        if _seq(a) not in kept_seqs:
            lineage.dropped[_seq(a)] = _why_dropped(a)  # type: ignore[index]

    i = 0  # the next kept action to match
    for step in lineage.steps:
        intent = step.get("intent") or ""
        kind = (step.get("do") or {}).get("kind")
        if kind == "assert" and intent.startswith(_REVIEW) and i < len(kept):
            lineage.by_turn.setdefault(_seq(kept[i]), []).append(Link(step, "check_before"))  # type: ignore[arg-type]
            continue
        if kind == "assert" and intent.startswith(_SIGHTED) and i > 0:
            lineage.by_turn.setdefault(_seq(kept[i - 1]), []).append(Link(step, "check_after"))  # type: ignore[arg-type]
            continue
        match = None
        for j in range(i, len(kept)):  # in order; skipping forward only past actions a hand edit removed
            act = kept[j].get("action") or {}
            if act.get("kind") == kind and intent_of(act.get("rationale") or "") == intent:
                match = j
                break
        if match is None:
            lineage.unmatched.append(step)
            continue
        a = kept[match]
        lineage.by_turn.setdefault(_seq(a), []).append(Link(step, "compiled"))  # type: ignore[arg-type]
        recorded, compiled = a.get("effect"), step.get("effect")
        if recorded and compiled and recorded != compiled:
            lineage.effect_changes[step["id"]] = (recorded, compiled)
        i = match + 1
    return lineage


def describe_step(step: dict[str, Any]) -> str:
    """One line for a compiled step's action: `click button_pay`, `assert target_equals_input(cell_amount, amount)`."""
    do = step.get("do") or {}
    kind = do.get("kind", "?")
    if kind == "navigate":
        return f"navigate {do.get('route', '')}"
    if kind == "type":
        return f"type {do.get('target')} <- {do.get('text')}"
    if kind == "select":
        return f"select {do.get('target')} <- {do.get('option')}"
    if kind == "extract":
        return f"extract {do.get('target')} -> {do.get('output')}"
    if kind == "press":
        return f"press {do.get('key')}"
    if kind == "assert":
        return f"assert {describe_predicate(do.get('that'))}" + (f" else {do['on_fail']}" if do.get("on_fail") else "")
    return f"{kind} {do.get('target') or ''}".rstrip()


def describe_predicate(p: dict[str, Any] | None) -> str:
    if not p:
        return ""
    kind = p.get("kind", "?")
    if kind == "all":
        return " and ".join(describe_predicate(x) for x in p.get("of", []))
    args = ", ".join(str(p[k]) for k in ("target", "input") if p.get(k))
    return f"{kind}({args})"


def describe_strategy(s: dict[str, Any]) -> str:
    """`role link "Enter payment"`, `same_row "Customer Code" -> value`, `css div.refid`."""
    kind = s.get("kind")
    if kind == "role":
        within = f' within {s["within_role"]} "{s.get("within_name") or ""}"' if s.get("within_role") else ""
        return f'role {s.get("role")} "{s.get("name") or ""}"{within}'
    if kind == "label":
        return f'label "{s.get("label")}"'
    if kind == "same_row":
        return f'same_row "{s.get("label")}" -> {s.get("take")}'
    if kind == "css":
        return f"css {s.get('css')}"
    rest = ", ".join(f"{k}={v}" for k, v in s.items() if k not in ("kind", "why", "matched_at_record") and v)
    return f"{kind} {rest}".strip()
