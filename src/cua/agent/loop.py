"""The discovery loop: observe, ask the model for ONE action, execute it through the guarded surface, record it.

- one action per turn; the adapter declares every tool on its first request and keeps an append-only history;
- every action is checked, resolved, recorded and performed by the `Executor`; this loop never touches the UI;
- a 30-turn budget (a turn without a usable action counts) and a wall-clock limit stop a run that goes nowhere, and so
  does the stuck detector (screen signature includes field values);
- everything observable lands in the run bundle through the evidence writer: events with rationales, masked
  screenshots, and (returned for the caller to persist) the transcript and the redacted-on-write trace.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from cua.agent.actions import AgentAction
from cua.agent.executor import Approver, Executor, Outcome
from cua.agent.model import Feedback, ModelAdapter, ModelTurn, Task, Usage, render_observation
from cua.agent.stuck import StuckDetector, screen_signature
from cua.artifact.schema import Profile
from cua.compiler.recorder import Recorder
from cua.compiler.trace import RecordedTrace, TargetRecord
from cua.control.lease import LeaseToken
from cua.evidence.events import EvidenceEvent, EvidenceWriter
from cua.safety.policy import Guard, PolicyDecision
from cua.surface.base import NativeDialogOpen, Observation, Surface, SurfaceError

MAX_STEPS = 30
WALL_CLOCK_S = 420.0


@dataclass
class LoopResult:
    trace: RecordedTrace
    outcome: str  # "done" | "stopped"
    stop_reason: str | None
    outputs: dict[str, str]  # extracted text per output name
    transcript: list[dict[str, Any]]
    usage: list[Usage] = field(default_factory=list)
    steps: int = 0


def _now() -> datetime:
    return datetime.now(UTC)


class AgentLoop:
    def __init__(
        self,
        *,
        surface: Surface,
        guard: Guard,
        lease: Callable[[], LeaseToken],
        evidence: EvidenceWriter,
        adapter: ModelAdapter,
        recorder: Recorder,
        profile: Profile,
        task: Task,
        inputs: Mapping[str, str],
        max_steps: int = MAX_STEPS,
        wall_clock_s: float = WALL_CLOCK_S,
        approve: Approver | None = None,
    ) -> None:
        self.surface, self.evidence, self.adapter, self.recorder, self.task = surface, evidence, adapter, recorder, task
        self.executor = Executor(surface=surface, guard=guard, lease=lease, recorder=recorder, profile=profile,
                                 inputs=inputs, outputs=task.outputs, approve=approve)
        self._lease = lease
        self._max_steps = max_steps
        self._wall_clock_s = wall_clock_s
        self._wants_image = bool(getattr(adapter, "screenshots", False))
        self._stuck = StuckDetector()
        self.transcript: list[dict[str, Any]] = []
        self._shots = 0

    # -- evidence ----------------------------------------------------------------------------------------------------

    def _event(self, kind: str, **fields: Any) -> None:
        self.evidence.event(EvidenceEvent(ts=_now(), seq=0, run_id="", actor="agent", kind=kind, **fields))

    async def _screenshot(self, label: str) -> None:
        """A masked screenshot of the current page (every input and cell masked)."""
        try:
            png = await self.surface.masked_screenshot([], generic=True)
            self._shots += 1
            self.evidence.image(f"screenshots/{self._shots:02d}-{label}.png", png)
        except (SurfaceError, NativeDialogOpen):
            pass

    # -- the loop ----------------------------------------------------------------------------------------------------

    async def _observe(self) -> Observation:
        return await self.surface.observe(screenshot=self._wants_image, settle=True)

    async def run(self) -> LoopResult:
        started = time.monotonic()
        usage: list[Usage] = []
        await self.adapter.start(self.task)
        obs = await self._observe()
        await self._screenshot("start")
        feedback: Feedback | None = None
        outcome, reason = "stopped", "budget"
        done_evidence: list[TargetRecord] = []
        step = 0

        while step < self._max_steps:
            remaining = self._wall_clock_s - (time.monotonic() - started)
            if remaining <= 0:
                reason = "wall_clock"
                break
            step += 1
            signature = screen_signature(obs)
            self._event("observe", step_id=f"t{step:02d}",
                        detail={"epoch": obs.epoch, "url": obs.facts.url, "signature": signature,
                                "dialog": obs.facts.dialog, "native_dialog": obs.facts.native_dialog})
            try:
                turn = await asyncio.wait_for(self.adapter.next_action(obs, feedback), timeout=remaining)
            except asyncio.TimeoutError:
                reason = "wall_clock"
                break
            usage.append(turn.usage)
            record: dict[str, Any] = {
                "seq": step, "observation": render_observation(obs), "feedback": feedback.message if feedback else None,
                "action": turn.action.model_dump(mode="json") if turn.action else None, "stop": turn.stop,
                "detail": turn.detail, "usage": turn.usage.model_dump(mode="json"),
            }
            self.transcript.append(record)
            if turn.stop == "refusal":
                self._event("model", step_id=f"t{step:02d}", detail={"stop": "refusal", "detail": turn.detail,
                                                                       "usage": turn.usage.model_dump()})
                reason = f"model_refusal: {turn.detail}"
                break
            if turn.stop == "error":
                self._event("model", step_id=f"t{step:02d}", detail={"stop": "error", "detail": turn.detail,
                                                                       "usage": turn.usage.model_dump()})
                reason = f"model_error: {turn.detail}"
                break
            if turn.action is None:
                message = ("Protocol error: reply with exactly one tool call. "
                           f"({turn.detail or 'no usable tool call'})")
                self._event("model", step_id=f"t{step:02d}", detail={"stop": turn.stop, "detail": turn.detail,
                                                                       "usage": turn.usage.model_dump()})
                feedback = Feedback(ok=False, message=message)
                record["outcome"] = {"status": "protocol_error", "message": message}
                continue

            action = turn.action
            key = json.dumps(action.model_dump(mode="json", exclude={"rationale"}), sort_keys=True)
            if why := self._stuck.check(signature, key):
                reason = f"stuck: {why}"
                break

            t0 = time.monotonic()
            started_at = _now()
            result = await self.executor.execute(action, obs)
            duration_ms = int((time.monotonic() - t0) * 1000)
            record["outcome"] = {"status": result.status, "message": result.message, "effect": result.effect}
            self._event(
                "action", step_id=f"t{step:02d}", action=action.model_dump(mode="json", exclude={"rationale"}),
                rationale=action.rationale, policy=result.policy, lease=self._lease(), duration_ms=duration_ms,
                detail={"status": result.status, "effect": result.effect, "tokens": result.tokens,
                        "message": result.message, "usage": turn.usage.model_dump()},
            )
            pre = self.recorder.screen(obs)

            if result.terminal:
                self.recorder.add(action, tokens=result.tokens, effect=result.effect, policy=_policy(result),
                                  status=result.status, error=None, target=result.target, pre=pre, post=None,
                                  sightings=[], started_at=started_at, duration_ms=duration_ms)
                if result.terminal == "done":
                    outcome, reason, done_evidence = "done", None, result.done_evidence
                else:
                    reason = ("help_requested" if result.terminal == "help"
                              else "approval_denied" if result.status == "blocked" else "approval_not_available")
                break

            post, sightings, obs = await self._after(result, obs)
            self.recorder.add(
                action, tokens=result.tokens, effect=result.effect, policy=_policy(result), status=result.status,  # type: ignore[arg-type]
                error=None if result.status == "performed" else result.message, target=result.target, pre=pre,
                post=post, sightings=sightings, started_at=started_at, duration_ms=duration_ms,
            )
            await self._screenshot(f"after-{step:02d}")
            feedback = Feedback(ok=result.status == "performed", message=result.message)

        trace = self.recorder.finish("done" if outcome == "done" else "stopped", reason, done_evidence)
        return LoopResult(trace=trace, outcome=outcome, stop_reason=reason, outputs=dict(self.executor.extracted),
                          transcript=self.transcript, usage=usage, steps=step)

    async def _after(self, result: Outcome, previous: Observation):
        """Observe after an action; find where inputs are displayed. Recording a target or a sighting runs `find()`,
        which drops the refs of the observation it ran on, so when it ran the model gets a fresh observation."""
        obs = await self._observe()
        sightings = []
        if result.status == "performed":
            wanted = self.recorder.sighting_refs(obs)
            if wanted:
                sightings = await self.recorder.sightings(obs, wanted)
                post = self.recorder.screen(obs)
                return post, sightings, await self.surface.observe(screenshot=self._wants_image)
        return self.recorder.screen(obs), sightings, obs


def _policy(result: Outcome) -> PolicyDecision:
    return result.policy
