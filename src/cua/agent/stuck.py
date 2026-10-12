"""Stuck detection: a screen signature that includes field values, and what counts as going in circles.

The signature is a hash of the URL, the dialog and the scoped snapshots with refs removed (refs change when a page
re-renders). The AI snapshot shows a textbox's value inline, so typing into a field changes the signature: filling a
form is progress, retyping the same thing is not.
"""

from __future__ import annotations

from collections import Counter

from cua.surface import snapshot as snap
from cua.surface.base import Observation


def screen_signature(obs: Observation) -> str:
    f = obs.facts
    return snap.signature(f.url, f.dialog, f.native_dialog, obs.snapshot, obs.dialog_snapshot)


class StuckDetector:
    """Flags a run that repeats itself.

    - the same action on the same screen `repeat_limit` times (it changed nothing, or came back to the same place);
    - the screen unchanged for `unchanged_limit` consecutive turns, whatever the actions were.
    """

    def __init__(self, *, repeat_limit: int = 3, unchanged_limit: int = 6) -> None:
        self.repeat_limit = repeat_limit
        self.unchanged_limit = unchanged_limit
        self._pairs: Counter[tuple[str, str]] = Counter()
        self._signatures: list[str] = []

    def check(self, signature: str, action_key: str) -> str | None:
        """Record one turn (the screen the model saw and the action it chose); return a reason when stuck."""
        self._signatures.append(signature)
        self._pairs[(signature, action_key)] += 1
        if self._pairs[(signature, action_key)] >= self.repeat_limit:
            return f"the same action was chosen {self.repeat_limit} times on an unchanged screen"
        tail = self._signatures[-self.unchanged_limit :]
        if len(tail) == self.unchanged_limit and len(set(tail)) == 1:
            return f"the screen did not change for {self.unchanged_limit} turns"
        return None
