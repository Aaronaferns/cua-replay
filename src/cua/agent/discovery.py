"""One discovery run end to end: loop -> run bundle -> compiled draft.

    runs/<run_id>/manifest.json, events.jsonl   (the evidence writer; rationales are in the action events)
                  transcript.json               what the model saw and chose, per turn (redacted on write)
                  trace.json                    the recorded trace, redacted on write: the compiler's only input
                  result.json                   outcome, stop reason, extracted outputs, token usage per turn
                  screenshots/*.png             masked
                  capability.draft.json         the compiled draft (or compile_error.json when it does not compile)
"""

from __future__ import annotations

import getpass
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cua.agent.loop import MAX_STEPS, WALL_CLOCK_S, AgentLoop, LoopResult
from cua.agent.model import ModelAdapter, Task
from cua.artifact.lock import dumps
from cua.artifact.schema import FieldSpec
from cua.compiler.compile import CompileError, compile_trace
from cua.compiler.recorder import Recorder
from cua.compiler.trace import RecordedTrace
from cua.control.runtime import Session


@dataclass
class DiscoveryResult:
    run_dir: Path
    loop: LoopResult
    compiled: bool
    compile_problems: list[str]
    draft_path: Path | None


def _jsonable(model: Any) -> Any:
    return model.model_dump(mode="json", by_alias=True)


async def discover(
    session: Session,
    adapter: ModelAdapter,
    *,
    capability: str,
    goal: str,
    input_values: Mapping[str, str],
    input_specs: Mapping[str, FieldSpec],
    output_specs: Mapping[str, FieldSpec],
    discovered_by: str | None = None,
    max_steps: int = MAX_STEPS,
    wall_clock_s: float = WALL_CLOCK_S,
) -> DiscoveryResult:
    evidence = session.evidence
    add = getattr(evidence, "add_secret_values", None)
    if add is not None:
        add(list(input_values.values()), label="input")  # every later write redacts the raw values

    entry_url = session.base_url + session.profile.entry.route
    task = Task(goal=goal, inputs=dict(input_specs), outputs=dict(output_specs), base_url=session.base_url)
    recorder = Recorder(
        session.surface, run_id=evidence.run_id, app=session.profile.app, goal=goal, model=adapter.name,  # type: ignore[attr-defined]
        inputs=input_specs, outputs=output_specs, input_values=input_values, entry_url=entry_url,
    )
    loop = AgentLoop(
        surface=session.surface, guard=session.guard, lease=session.lease, evidence=evidence, adapter=adapter,
        recorder=recorder, profile=session.profile, task=task, inputs=input_values, max_steps=max_steps,
        wall_clock_s=wall_clock_s,
    )
    result = await loop.run()

    evidence.json("transcript.json", {"goal": goal, "model": adapter.name, "turns": result.transcript})
    trace_path = evidence.json("trace.json", _jsonable(result.trace))
    evidence.json("result.json", {
        "capability": capability, "outcome": result.outcome, "stop_reason": result.stop_reason, "steps": result.steps,
        "outputs": result.outputs, "model": adapter.name,
        "usage": [{"turn": i, **u.model_dump()} for i, u in enumerate(result.usage, start=1)],
        "usage_total": {k: sum(getattr(u, k) for u in result.usage) for k in
                        ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")},
    })

    # the compiler works from the persisted (redacted) trace alone
    persisted = RecordedTrace.model_validate_json(trace_path.read_text(encoding="utf-8"))
    draft_path: Path | None = None
    problems: list[str] = []
    try:
        capability_doc = compile_trace(persisted, session.profile, name=capability,
                                       discovered_by=discovered_by or getpass.getuser())
        draft_path = evidence.text("capability.draft.json", dumps(capability_doc))
    except CompileError as e:
        problems = list(e.problems)
        evidence.json("compile_error.json", {"problems": problems})
    return DiscoveryResult(run_dir=evidence.run_dir, loop=result, compiled=draft_path is not None,
                           compile_problems=problems, draft_path=draft_path)
