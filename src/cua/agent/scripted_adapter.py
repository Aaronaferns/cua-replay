"""A model adapter that replays a fixed list of actions: for tests, and for re-running a recorded transcript.

A transcript is the `transcript.json` the loop writes: `{"turns": [{"action": {...} | null, ...}, ...]}`. Turns without
an action (protocol problems) are skipped. When the script runs out the adapter reports a protocol problem, so a loop
that never finishes stops at its budget.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from pathlib import Path

from pydantic import TypeAdapter

from cua.agent.actions import AgentAction
from cua.agent.model import Feedback, ModelTurn, Task, Usage
from cua.surface.base import Observation

_ACTION = TypeAdapter(AgentAction)


class ScriptedAdapter:
    name = "scripted"

    def __init__(self, actions: Iterable[AgentAction | Callable[[Observation], AgentAction]]):
        """Each item is an action, or a function of the current observation that returns one (tests use these to
        pick refs by role and name, since refs are only known at run time)."""
        self._actions = list(actions)
        self._next = 0
        self.task: Task | None = None
        self.observations: list[Observation] = []
        self.feedback: list[Feedback | None] = []

    @classmethod
    def from_transcript(cls, path: Path | str) -> ScriptedAdapter:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        turns = data["turns"] if isinstance(data, dict) else data
        return cls(_ACTION.validate_python(t["action"]) for t in turns if t.get("action"))

    async def start(self, task: Task) -> None:
        self.task = task
        self._next = 0

    async def next_action(self, obs: Observation, feedback: Feedback | None = None) -> ModelTurn:
        self.observations.append(obs)
        self.feedback.append(feedback)
        if self._next >= len(self._actions):
            return ModelTurn(stop="no_action", detail="the script has no more actions", usage=Usage())
        step = self._actions[self._next]
        self._next += 1
        action = step(obs) if callable(step) else step
        return ModelTurn(action=action, usage=Usage())
