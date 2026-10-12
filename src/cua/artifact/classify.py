"""Screen classification: the profile's state catalog applied to one observed screen.

Pure and context-free: the same screen and profile always give the same answer, with no knowledge of the run, the
step or the inputs. The dialog layer is matched first (only dialog-layer states, only against the dialog nodes);
otherwise (no dialog open) page-layer states are matched against the page. A state that `specializes` another wins
over its parent when both match; any other overlap is `ambiguous` and must fail closed. A screen that matches nothing
is `unknown` and carries the closest states as near misses.

Leaf module: pydantic models from this package and the standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from cua.artifact.predicates import ScreenView, clause_score, holds
from cua.artifact.schema import Profile

Kind = Literal["state", "unknown", "ambiguous"]
MAX_NEAR_MISSES = 3


@dataclass(frozen=True)
class Classification:
    kind: Kind
    state: str | None = None  # set when kind == "state"
    candidates: tuple[str, ...] = ()  # set when kind == "ambiguous": the states that all matched
    near_misses: tuple[str, ...] = ()  # set when kind == "unknown": "state (satisfied/total)"

    @property
    def label(self) -> str:
        """The state id, or "unknown" / "ambiguous" (what a result reports as the observed state)."""
        return self.state if self.kind == "state" and self.state else self.kind


def _matching(profile: Profile, layer: str, view: ScreenView) -> list[str]:
    matched = [sid for sid, st in profile.states.items() if st.layer == layer and holds(st.signature, view)]
    # a parent is shadowed by any matching specialization of it
    return sorted(s for s in matched if not any(o != s and profile.is_a(o, s) for o in matched))


def _near_misses(profile: Profile, layers: set[str], page: ScreenView, dialog: ScreenView | None) -> tuple[str, ...]:
    scored: list[tuple[float, str, str]] = []
    for sid, st in profile.states.items():
        if st.layer not in layers:
            continue
        done, total = clause_score(st.signature, dialog if st.layer == "dialog" and dialog is not None else page)
        if done:
            scored.append((-done / total, sid, f"{sid} ({done}/{total})"))
    return tuple(label for _, _, label in sorted(scored)[:MAX_NEAR_MISSES])


def classify(profile: Profile, page: ScreenView, dialog: ScreenView | None = None) -> Classification:
    if dialog is not None:
        found = _matching(profile, "dialog", dialog)
        if len(found) == 1:
            return Classification("state", state=found[0])
        if found:
            return Classification("ambiguous", candidates=tuple(found))
        # an open dialog that matches no dialog state is an unknown dialog, never the page underneath it
        return Classification("unknown", near_misses=_near_misses(profile, {"dialog"}, page, dialog))
    found = _matching(profile, "page", page)
    if len(found) == 1:
        return Classification("state", state=found[0])
    if found:
        return Classification("ambiguous", candidates=tuple(found))
    return Classification("unknown", near_misses=_near_misses(profile, {"page"}, page, dialog))
