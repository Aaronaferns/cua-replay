"""The loop on the offline Dolibarr-shaped site: budget, stuck detection, a scripted run, recording and compiling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fakesite import LEASE, ORIGIN
from scripts import GOAL, INPUT_SPECS, OUTPUT_SPECS, code_filter_ref, ref_of

from cua.agent.actions import Click, Done, Extract, Navigate, Press, Type
from cua.agent.discovery import discover
from cua.agent.executor import Executor
from cua.agent.loop import AgentLoop
from cua.agent.model import Task
from cua.agent.scripted_adapter import ScriptedAdapter
from cua.artifact.lock import load_profile
from cua.compiler.recorder import Recorder
from cua.evidence.writer import RunBundleWriter
from cua.surface.web import WebSurface

pytestmark = pytest.mark.asyncio(loop_scope="module")

PROFILE = load_profile(Path(__file__).resolve().parents[2] / "apps" / "dolibarr" / "profile.json")
CODE = "CU1212-0005"  # the code on the fake site's customer card


def build(surface, guard, tmp_path, adapter, **kw):
    writer = RunBundleWriter("t1", root=tmp_path)
    writer.add_secret_values([CODE])
    task = Task(goal=GOAL, inputs=INPUT_SPECS, outputs=OUTPUT_SPECS, base_url=ORIGIN)
    recorder = Recorder(surface, run_id="t1", app="dolibarr", goal=GOAL, model="scripted", inputs=INPUT_SPECS,
                        outputs=OUTPUT_SPECS, input_values={"customer_code": CODE}, entry_url=ORIGIN + "/index.php")
    loop = AgentLoop(surface=surface, guard=guard, lease=lambda: LEASE, evidence=writer, adapter=adapter,
                     recorder=recorder, profile=PROFILE, task=task, inputs={"customer_code": CODE}, **kw)
    return loop, writer


def card_link(obs):
    return ref_of(obs, "link", "Northwind Test")


def script():
    return [
        Navigate(rationale="Open the customer list", url=ORIGIN + "/societe/list.php?type=c"),
        lambda obs: Type(rationale="Filter by {{customer_code}}", ref=code_filter_ref(obs), text="{{customer_code}}"),
        Press(rationale="Apply the filter", key="Enter"),
        lambda obs: Click(rationale="Open the customer", ref=card_link(obs)),
        lambda obs: Extract(rationale="Read the amount", name="outstanding_amount",
                            ref=ref_of(obs, "generic", "Current outstanding bill")),
        lambda obs: Done(rationale="Done", evidence_refs=[ref_of(obs, "generic", "Current outstanding bill")]),
    ]


async def test_scripted_adapter_loop(fake_surface, tmp_path):
    surface, page, guard = fake_surface
    loop, writer = build(surface, guard, tmp_path, ScriptedAdapter(script()))
    result = await loop.run()

    assert result.outcome == "done" and result.stop_reason is None
    assert "75.37" in result.outputs["outstanding_amount"]
    kinds = [a.action.kind for a in result.trace.actions]
    assert kinds == ["navigate", "type", "press", "click", "extract", "done"]
    assert all(a.status == "performed" for a in result.trace.actions)
    # one guard decision per surface action (navigate, type, press, click) plus the guarded extract
    assert [c.kind for c in guard.calls[-5:]] == ["navigate", "type", "press", "click", "extract"]
    assert all(c.actor == "agent" for c in guard.calls[-5:])

    typed = result.trace.actions[1]
    assert typed.action.text == "{{customer_code}}" and typed.tokens == ["customer_code"]
    assert typed.effect == "none" and typed.target is not None and typed.target.candidates
    # the click lands on the card, where the code is shown next to its label: a sighting with a same_row locator
    sighted = [s for a in result.trace.actions for s in a.sightings]
    assert [s.token for s in sighted] == ["customer_code"]
    assert any(c.rank == 2 and c.strategy.kind == "same_row" and c.strategy.label == "Customer Code"
               for c in sighted[0].target.candidates)
    assert result.trace.done_evidence and result.trace.done_evidence[0].candidates

    events = [json.loads(line) for line in (writer.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["rationale"] for e in events if e["kind"] == "action"][:2] == [
        "Open the customer list", "Filter by {{customer_code}}"]
    assert CODE not in (writer.run_dir / "events.jsonl").read_text(encoding="utf-8")
    assert list((writer.run_dir / "screenshots").glob("*.png"))
    writer.close()


async def test_loop_stops_on_budget(fake_surface, tmp_path):
    surface, page, guard = fake_surface
    # turns that yield no usable action count against the budget, so a model that never acts cannot run forever
    loop, writer = build(surface, guard, tmp_path, ScriptedAdapter([]), max_steps=4)
    result = await loop.run()
    assert result.outcome == "stopped" and result.stop_reason == "budget" and result.steps == 4
    assert result.trace.actions == []
    # the model is told what went wrong each time
    assert [t["outcome"]["status"] for t in result.transcript] == ["protocol_error"] * 4
    writer.close()


async def test_loop_stops_on_budget_with_real_actions(fake_surface, tmp_path):
    surface, page, guard = fake_surface
    keys = ["Tab", "Shift+Tab", "Escape", "Home", "End", "PageDown"]
    loop, writer = build(surface, guard, tmp_path,
                         ScriptedAdapter([Press(rationale="try", key=k) for k in keys]), max_steps=3)
    result = await loop.run()
    assert result.stop_reason == "budget" and len(result.trace.actions) == 3
    writer.close()


async def test_default_budget_is_thirty_steps():
    from cua.agent.loop import MAX_STEPS

    assert MAX_STEPS == 30


async def test_loop_stops_on_wall_clock(fake_surface, tmp_path):
    surface, page, guard = fake_surface
    loop, writer = build(surface, guard, tmp_path, ScriptedAdapter(script()), wall_clock_s=0.0)
    result = await loop.run()
    assert result.outcome == "stopped" and result.stop_reason == "wall_clock"
    writer.close()


async def test_stuck_detector_stops_a_repeating_run(fake_surface, tmp_path):
    surface, page, guard = fake_surface
    loop, writer = build(surface, guard, tmp_path, ScriptedAdapter([Press(rationale="again", key="Escape")] * 10))
    result = await loop.run()
    assert result.stop_reason.startswith("stuck")
    assert len(result.trace.actions) == 2  # the third identical choice on the unchanged screen is not executed
    writer.close()


async def test_rejected_and_denied_actions_are_recorded_not_performed(fake_surface, tmp_path):
    surface, page, guard = fake_surface
    steps = [
        Type(rationale="leaks the value", ref="e1", text=CODE),
        Navigate(rationale="off the app", url="file:///etc/passwd"),
        Done(rationale="too early", evidence_refs=["e1"]),
    ]
    loop, writer = build(surface, guard, tmp_path, ScriptedAdapter(steps), max_steps=3)
    result = await loop.run()
    assert [a.status for a in result.trace.actions] == ["error", "error", "error"]
    assert not any(c.kind == "type" for c in guard.calls)  # rejected before the guard, UI untouched
    assert "extract these outputs first" in result.trace.actions[2].error
    writer.close()


async def test_persisted_trace_is_redacted_and_loads(fake_surface, tmp_path):
    """The trace is written through the evidence writer (redacted) and still loads as a `RecordedTrace`; the
    compiler's input is that copy. (Compiling needs real Dolibarr screens: see test_discovery_live.)"""
    from cua.compiler.trace import RecordedTrace

    surface, page, guard = fake_surface
    loop, writer = build(surface, guard, tmp_path, ScriptedAdapter(script()))
    result = await loop.run()
    path = writer.json("trace.json", result.trace.model_dump(mode="json", by_alias=True))
    persisted = path.read_text(encoding="utf-8")
    assert CODE not in persisted and "{{customer_code}}" in persisted
    trace = RecordedTrace.model_validate_json(persisted)
    assert [a.action.kind for a in trace.actions] == [a.action.kind for a in result.trace.actions]
    assert trace.actions[0].pre.snapshot and "Customer Code" in trace.actions[3].post.snapshot
    writer.close()
