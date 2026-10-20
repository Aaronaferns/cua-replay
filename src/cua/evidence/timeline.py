"""One run bundle read back as a timeline: the shared model behind `cua view` (HTML) and `cua export-otel` (OTLP).

Reads only what the evidence writer already wrote, and therefore already redacted. Never writes into the bundle: the
manifest's hashes cover every file in it.

Shape:

    Timeline     the run: goal or capability, status/outcome, start/end, usage, outputs, failure
    Segment      the events of one step (`step_id`), in order; events outside a step that set up the run (session,
                 login, entry navigation) form their own segment, and other step-less events (faults, policy) join
                 the segment they happened in
    Handoff      one human handoff, from `request_human` to the lease coming back (or `handoff_ended`): reason,
                 intervention, operator, what the human did, how it ended
    lineage      discovery only: which step of the compiled draft came from which turn (`cua.evidence.lineage`)
    network      the system's request-level policy checks (every sub-resource the page loads); kept apart because
                 they dominate the event count and say little about the run unless one was denied

Lease, intervention and human events go to the handoff, not the segment grouping, so a step that waited for an
approval stays one segment that spans the wait.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from cua.evidence.lineage import Lineage
from cua.evidence.lineage import build as build_lineage

_HANDOFF_KINDS = {"lease", "intervention", "escalated", "handoff_ended"}
_PHASE_KINDS = {"state", "action", "observe"}  # step-less events of these kinds start a run-level segment
_SCREENSHOT = re.compile(r"(\d+)-")
_STEP_NUMBER = re.compile(r"t(\d+)$")


class TimelineError(Exception):
    """The folder is not a readable run bundle; the message names the problem, never file content."""


@dataclass
class Segment:
    key: str  # the step id, or "run:<n>" for a run-level segment
    step_id: str | None
    actor: str
    start: datetime
    end: datetime
    events: list[dict[str, Any]] = field(default_factory=list)
    screenshot: str | None = None  # relative path inside the bundle
    images: list[str] = field(default_factory=list)  # failure evidence for this step
    turn: dict[str, Any] | None = None  # the model turn (discovery)
    target: dict[str, Any] | None = None  # the element the action resolved to (discovery trace)

    @property
    def duration_ms(self) -> int:
        return max(0, int((self.end - self.start).total_seconds() * 1000))

    @property
    def action(self) -> dict[str, Any] | None:
        for e in reversed(self.events):
            if e["kind"] == "action" and e.get("action"):
                return e
        return None

    @property
    def label(self) -> str:
        if self.step_id:
            return self.step_id
        kinds = []
        for e in self.events:
            d = e.get("detail") or {}
            name = (
                f"session {d['session']}" if e["kind"] == "state" and d.get("session")
                else "entry" if (e.get("action") or {}).get("entry")
                else None
            )
            if name and name not in kinds:
                kinds.append(name)
        return ", ".join(kinds) or "run"

    @property
    def states(self) -> list[str]:
        out: list[str] = []
        for e in self.events:
            if e["kind"] == "state" and e.get("state") and (not out or out[-1] != e["state"]):
                out.append(e["state"])
        return out

    @property
    def effect(self) -> str | None:
        effects = [(e.get("detail") or {}).get("effect") for e in self.events if e["kind"] == "action"]
        return "commit" if "commit" in effects else next((x for x in effects if x), None)

    @property
    def commit_attempted(self) -> bool:
        return any(e["kind"] == "commit_attempted" for e in self.events)

    @property
    def status(self) -> str:
        """ok | denied | error | escalated (the worst one seen)."""
        status = "ok"
        for e in self.events:
            policy = e.get("policy") or {}
            if e["kind"] == "escalated":
                return "escalated"
            if (e.get("detail") or {}).get("status") == "error":
                status = "error"
            elif policy.get("allowed") is False and policy.get("rule") != "not_evaluated" and status == "ok":
                status = "denied"
        return status


@dataclass
class Handoff:
    reason: str
    start: datetime
    end: datetime | None = None
    step_id: str | None = None
    intervention: str | None = None
    request: dict[str, Any] | None = None  # interventions/<id>.json
    sla_deadline: str | None = None
    operator: str | None = None
    claimed: datetime | None = None
    decisions: list[dict[str, Any]] = field(default_factory=list)  # the operator's console actions
    human_actions: list[dict[str, Any]] = field(default_factory=list)  # what the operator did in the page
    outcome: str | None = None  # resume | verdict | approve ...
    verdict: str | None = None
    generations: list[int] = field(default_factory=list)

    @property
    def duration_ms(self) -> int:
        return max(0, int(((self.end or self.start) - self.start).total_seconds() * 1000))

    @property
    def wait_ms(self) -> int | None:
        """From the request to a human claiming it."""
        return None if self.claimed is None else max(0, int((self.claimed - self.start).total_seconds() * 1000))


@dataclass
class Timeline:
    root: Path
    run_id: str
    kind: str  # discovery | replay
    title: str
    capability: str | None
    model: str | None
    status: str | None
    outcome: str | None
    start: datetime
    end: datetime
    events: list[dict[str, Any]]
    segments: list[Segment]
    handoffs: list[Handoff]
    network: list[dict[str, Any]]
    result: dict[str, Any]
    manifest: dict[str, Any]
    trace: dict[str, Any] | None
    ground_truth: dict[str, Any] | None
    lineage: Lineage | None = None  # discovery: which compiled step came from which turn

    @property
    def duration_ms(self) -> int:
        return max(0, int((self.end - self.start).total_seconds() * 1000))

    @property
    def outputs(self) -> dict[str, Any]:
        return dict(self.result.get("outputs") or {})

    @property
    def failure(self) -> dict[str, Any] | None:
        return self.result.get("failure")

    @property
    def usage(self) -> dict[str, int]:
        total = self.result.get("usage_total") or {}
        return {k: int(v) for k, v in total.items() if isinstance(v, int)}

    @property
    def denied(self) -> list[dict[str, Any]]:
        return [e for e in self.network if (e.get("policy") or {}).get("allowed") is False]


def turn_number(step_id: str | None) -> int | None:
    """The discovery turn of a step id (`t08` -> 8); None for replay steps and run-level segments."""
    m = _STEP_NUMBER.match(step_id or "")
    return int(m.group(1)) if m else None


def describe_action(action: dict[str, Any] | None, target: dict[str, Any] | None = None) -> str:
    """One line for an action of any actor: `click button "Pay"`, `type textbox_payment_amount <- ${amount}`."""
    if not action:
        return ""
    kind = action.get("kind", "?")
    if target and (target.get("role") or target.get("name")):
        what = f'{target.get("role") or ""} "{target.get("name") or ""}"'.strip()
    else:
        what = action.get("target") or action.get("ref") or action.get("label") or action.get("name") or ""
    if kind == "navigate":
        return "navigate (entry)" if action.get("entry") else f"navigate {action.get('route') or action.get('url') or ''}"
    if kind in ("type", "change"):
        value = action.get("text", action.get("value"))
        return f"{kind} {what}" + (f" <- {value}" if value is not None else "")
    if kind == "select":
        return f"select {what} <- {action.get('option')}"
    if kind == "extract":
        return f"extract {what} -> {action.get('output') or action.get('name')}"
    if kind == "assert":
        that = action.get("that") or {}
        args = ", ".join(str(that[k]) for k in ("target", "input") if that.get(k))
        return f"assert {that.get('kind', '?')}({args})" + (f" else {action['on_fail']}" if action.get("on_fail") else "")
    if kind == "submit":
        return f"submit form {action.get('name') or ''}".rstrip()
    return f"{kind} {what}".rstrip()


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load(root: Path) -> Timeline:
    root = Path(root)
    events_file = root / "events.jsonl"
    if not events_file.is_file():
        raise TimelineError(f"{root} has no events.jsonl: not a run bundle")
    events: list[dict[str, Any]] = []
    for n, line in enumerate(events_file.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            e = json.loads(line)
            e["_ts"] = _ts(e["ts"])
        except (ValueError, KeyError, TypeError):
            raise TimelineError(f"events.jsonl line {n} is not an evidence event") from None
        events.append(e)
    if not events:
        raise TimelineError("events.jsonl is empty")
    events.sort(key=lambda e: e["seq"])

    manifest = _read_json(root / "manifest.json") or {}
    result = _read_json(root / "result.json") or {}
    trace = _read_json(root / "trace.json")
    transcript = _read_json(root / "transcript.json")
    discovery = trace is not None or any(e["actor"] == "agent" for e in events)

    network = [e for e in events if e["actor"] == "system" and e["kind"] == "policy"]
    segments = _segments([e for e in events if not (e["actor"] == "system" and e["kind"] == "policy")])
    _attach_media(root, segments, result, trace, transcript)
    handoffs = _handoffs(root, events, segments)

    start = min(e["_ts"] for e in events)
    end = max(e["_ts"] for e in events)
    capability = result.get("capability")
    if discovery:
        title = (trace or transcript or {}).get("goal") or capability or manifest.get("run_id", root.name)
        status, outcome = result.get("outcome"), result.get("stop_reason") or result.get("outcome")
    else:
        title = capability or root.name
        status, outcome = result.get("status"), result.get("outcome")
    return Timeline(
        root=root,
        run_id=manifest.get("run_id") or events[0].get("run_id") or root.name,
        kind="discovery" if discovery else "replay",
        title=title,
        capability=capability,
        model=result.get("model") or (trace or {}).get("model"),
        status=status,
        outcome=outcome,
        start=start,
        end=end,
        events=events,
        segments=segments,
        handoffs=handoffs,
        network=network,
        result=result,
        manifest=manifest,
        trace=trace,
        ground_truth=_read_json(root / "ground_truth.json"),
        lineage=build_lineage(trace, _read_json(root / "capability.draft.json")) if discovery else None,
    )


def _segments(events: list[dict[str, Any]]) -> list[Segment]:
    out: list[Segment] = []
    runs = 0
    for e in events:
        handoff = e["kind"] in _HANDOFF_KINDS or e["actor"] == "human"
        step = e.get("step_id")
        current = out[-1] if out else None
        if handoff or e["kind"] == "result":
            if current is not None and step and current.step_id == step:
                current.events.append(e)  # kept for the step's detail; does not decide grouping
            continue
        if current is not None and (
            current.step_id == step  # the same step, or a run-level segment continuing
            or (step is None and e["kind"] not in _PHASE_KINDS)  # a fault or check inside the current step
        ):
            current.events.append(e)
            current.start, current.end = min(current.start, e["_ts"]), max(current.end, e["_ts"])
            continue
        if step is None:
            runs += 1
        out.append(Segment(key=step or f"run:{runs}", step_id=step, actor=e["actor"], start=e["_ts"], end=e["_ts"],
                           events=[e]))
    for seg in out:
        actors = [e["actor"] for e in seg.events if e["actor"] != "system"]
        seg.actor = actors[0] if actors else "system"
    return out


def _attach_media(root: Path, segments: list[Segment], result: dict[str, Any], trace: dict[str, Any] | None,
                  transcript: dict[str, Any] | None) -> None:
    shots = {}
    if (root / "screenshots").is_dir():
        for p in sorted((root / "screenshots").glob("*.png")):
            if m := _SCREENSHOT.match(p.name):
                shots.setdefault(int(m.group(1)), p.relative_to(root).as_posix())
    turns = {int(t["seq"]): t for t in (transcript or {}).get("turns", []) if str(t.get("seq", "")).isdigit()}
    targets = {int(a["seq"]): a.get("target") for a in (trace or {}).get("actions", []) if str(a.get("seq", "")).isdigit()}
    failure = result.get("failure") or {}
    for seg in segments:
        if seg.step_id and (m := _STEP_NUMBER.match(seg.step_id)):
            n = int(m.group(1))
            seg.screenshot, seg.turn, seg.target = shots.get(n), turns.get(n), targets.get(n)
        if failure.get("step_id") and failure["step_id"] == seg.step_id:
            seg.images += [p for p in failure.get("evidence", []) if p.endswith(".png") and (root / p).is_file()]


def _handoffs(root: Path, events: list[dict[str, Any]], segments: list[Segment]) -> list[Handoff]:
    out: list[Handoff] = []
    current: Handoff | None = None
    escalated_step: str | None = None
    for e in events:
        d, ts = e.get("detail") or {}, e["_ts"]
        transition = d.get("transition") if e["kind"] == "lease" else None
        if e["kind"] == "escalated":
            escalated_step = e.get("step_id")
        if transition == "request_human":
            current = Handoff(reason=d.get("reason") or "?", start=ts, step_id=e.get("step_id") or escalated_step)
            current.generations.append(d.get("generation"))
            out.append(current)
            escalated_step = None
            continue
        target = current or (out[-1] if out else None)
        if target is None:
            continue
        if e["kind"] == "intervention" and e["actor"] == "system":
            target.intervention = d.get("id")
            target.sla_deadline = d.get("sla_deadline")
            target.step_id = target.step_id or e.get("step_id")
            target.start = min(target.start, ts)
            if d.get("request"):
                target.request = _read_json(root / d["request"])
        elif transition == "claim" and current is not None:
            current.claimed, current.operator = ts, d.get("operator")
            current.generations.append(d.get("generation"))
        elif e["kind"] == "intervention" and e["actor"] == "human":  # logged just after the lease moves, too
            if d.get("action") != "claim":
                target.decisions.append({"ts": ts, **{k: v for k, v in d.items() if v is not None}})
                target.verdict = d.get("verdict") or target.verdict
        elif e["kind"] == "action" and e["actor"] == "human":
            target.human_actions.append({"ts": ts, **(e.get("action") or {})})
        elif transition == "release_to_automation" and current is not None:
            current.end, current.outcome = ts, d.get("reason")
            current.generations.append(d.get("generation"))
            current = None
        elif e["kind"] == "handoff_ended":
            target.end = max(target.end or ts, ts)
            target.outcome = d.get("outcome") or target.outcome
            target.verdict = d.get("verdict") or target.verdict
            current = None
    for h in out:
        if h.end is None:
            h.end = max(e["_ts"] for e in events)
        if h.step_id is None:  # discovery asks for approval mid-step: the step whose span holds the request
            h.step_id = next((s.step_id for s in segments if s.step_id and s.start <= h.start <= s.end), None)
        h.generations = [g for g in h.generations if g is not None]
    return out
