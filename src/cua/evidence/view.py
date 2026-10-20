"""`cua view`: render a run bundle (or a folder of them) as one self-contained HTML trace.

    cua view evidence/replay_commit_unknown            -> runs/views/replay_commit_unknown.html
    cua view evidence/                                 -> one page per bundle + runs/views/index.html
    cua view <bundle> --out trace.html --open

The page is built from the bundle alone (`cua.evidence.timeline`), which the evidence writer already redacted, and
is written outside it: the bundle's manifest hashes every file, so nothing is added there. Screenshots are embedded
as data URIs so the file can be sent on its own; every string from the bundle is HTML-escaped and the page loads
nothing from the network.

What it shows: a waterfall of steps and human handoffs on one clock (color = who acted, outline = how it went), then
one card per step (action, the model's stated rationale, target, policy, lease, screenshot, what the model saw) and
per handoff (reason, wait, operator, decisions, captured human actions), the request-level policy checks, and the
manifest.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import sys
import webbrowser
from pathlib import Path
from typing import Any

from cua.evidence.lineage import Lineage, Link, describe_predicate, describe_step, describe_strategy
from cua.evidence.timeline import Handoff, Segment, Timeline, TimelineError, describe_action, load, turn_number

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_OBSERVATION_CHARS = 12_000


def e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _ms(ms: int | None) -> str:
    if ms is None:
        return "-"
    if ms < 1000:
        return f"{ms} ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f} s"
    return f"{ms // 60_000} min {ms % 60_000 / 1000:.0f} s"


def _image(tl: Timeline, rel: str | None) -> str:
    """A data URI for a PNG inside the bundle, or "" (a path outside the bundle or a non-PNG is never embedded)."""
    if not rel:
        return ""
    path = (tl.root / rel).resolve()
    if not path.is_relative_to(tl.root.resolve()) or not path.is_file():
        return ""
    data = path.read_bytes()
    if not data.startswith(_PNG_MAGIC):
        return ""
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def _pre_json(obj: Any) -> str:
    clean = obj
    if isinstance(obj, dict):
        clean = {k: v for k, v in obj.items() if not k.startswith("_")}
    elif isinstance(obj, list):
        clean = [{k: v for k, v in x.items() if not k.startswith("_")} if isinstance(x, dict) else x for x in obj]
    return f"<pre>{e(json.dumps(clean, indent=2, ensure_ascii=False, default=str))}</pre>"


def _status_class(value: str | None) -> str:
    v = (value or "").lower()
    if v in ("success", "done", "ok", "resume", "operator_resume", "operator_approve", "committed"):
        return "good"
    if v in ("indeterminate", "escalated", "commit_unknown", "verdict"):
        return "warn"
    if v in ("failure", "error", "denied", "policy_denied") or v.isupper():
        return "bad"
    return "neutral"


# ------------------------------------------------------------------------------------------------- waterfall


def _rows(tl: Timeline) -> list[tuple[str, Segment | Handoff]]:
    """Segments in order, each handoff right after the step it belongs to (or after the segment it falls in)."""
    rows: list[tuple[str, Segment | Handoff]] = []
    placed: set[int] = set()
    for seg in tl.segments:
        rows.append(("seg", seg))
        for i, h in enumerate(tl.handoffs):
            if i in placed:
                continue
            if (h.step_id and h.step_id == seg.step_id) or (not h.step_id and seg.start <= h.start <= seg.end):
                rows.append(("handoff", h))
                placed.add(i)
    rows += [("handoff", h) for i, h in enumerate(tl.handoffs) if i not in placed]
    return rows


def _dur(left: float, width: float, ms: int) -> str:
    if left + width > 85:
        return f'<span class="dur" style="right:calc({100 - left:.3f}% + 4px)">{e(_ms(ms))}</span>'
    return f'<span class="dur" style="left:calc({left + width:.3f}% + 4px)">{e(_ms(ms))}</span>'


def _pos(tl: Timeline, start, end) -> tuple[float, float]:
    total = max(tl.duration_ms, 1)
    left = (start - tl.start).total_seconds() * 1000 / total * 100
    width = max((end - start).total_seconds() * 1000 / total * 100, 0.35)
    left = min(max(left, 0.0), 99.65)
    return left, min(width, 100 - left)


def _waterfall(tl: Timeline) -> str:
    ticks = "".join(
        f'<span class="tick" style="left:{p}%">{e(_ms(int(tl.duration_ms * p / 100)))}</span>' for p in (0, 25, 50, 75, 100)
    )
    out = [f'<div class="wf"><div class="wf-row wf-axis"><div class="wf-label"></div><div class="wf-track">{ticks}</div></div>']
    for kind, item in _rows(tl):
        if kind == "seg":
            seg: Segment = item  # type: ignore[assignment]
            left, width = _pos(tl, seg.start, seg.end)
            action = seg.action
            summary = describe_action(action.get("action") if action else None, seg.target)
            marks = (' <span class="mark commit" title="commit effect">commit</span>' if seg.effect == "commit" else "")
            out.append(
                f'<a class="wf-row" href="#{e(_anchor(seg.key))}">'
                f'<div class="wf-label"><b>{e(seg.label)}</b>{marks}<span class="sub">{e(summary)}</span></div>'
                f'<div class="wf-track"><span class="bar actor-{e(seg.actor)} st-{e(seg.status)}" '
                f'style="left:{left:.3f}%;width:{width:.3f}%" title="{e(seg.label)}: {e(_ms(seg.duration_ms))}"></span>'
                f'{_dur(left, width, seg.duration_ms)}</div></a>'
            )
        else:
            h: Handoff = item  # type: ignore[assignment]
            left, width = _pos(tl, h.start, h.end or h.start)
            claimed = h.claimed or h.end or h.start  # hatched until a human claims it, solid while they hold it
            wl, ww = _pos(tl, h.start, claimed)
            hl, hw = _pos(tl, claimed, h.end or claimed)
            bars = (f'<span class="bar handoff-wait" style="left:{wl:.3f}%;width:{ww:.3f}%" title="waiting for a human: '
                    f'{e(_ms(h.wait_ms))}"></span>' if h.claimed is not None else "")
            bars += (f'<span class="bar handoff" style="left:{hl:.3f}%;width:{hw:.3f}%" title="human holds the lease"></span>')
            out.append(
                f'<a class="wf-row handoff-row" href="#{e(_anchor("handoff-" + (h.intervention or str(id(h)))))}">'
                f'<div class="wf-label"><b>handoff</b> <span class="mark human">{e(h.reason)}</span>'
                f'<span class="sub">{e(h.operator or "unclaimed")} · {e(h.outcome or "open")}'
                f'{" · " + e(h.verdict) if h.verdict else ""}</span></div>'
                f'<div class="wf-track">{bars}{_dur(left, width, h.duration_ms)}</div></a>'
            )
    out.append("</div>")
    legend = "".join(
        f'<span><i class="sw {c}"></i>{e(t)}</span>'
        for c, t in (("actor-agent", "model (discovery)"), ("actor-replay", "replay (no model)"), ("actor-human", "human"),
                     ("actor-system", "system"), ("handoff-wait", "waiting for a human"), ("handoff", "human holds the lease"),
                     ("st-error", "error / denied"), ("st-escalated", "escalated"))
    )
    return f'<div class="legend">{legend}</div>' + "".join(out)


# ---------------------------------------------------------------------------------------------------- cards


def _anchor(key: str) -> str:
    return "s-" + "".join(c if c.isalnum() or c in "-_" else "-" for c in key)


def _facts(rows: list[tuple[str, Any]]) -> str:
    body = "".join(f"<tr><th>{e(k)}</th><td>{v}</td></tr>" for k, v in rows if v not in (None, "", []))
    return f'<table class="facts">{body}</table>'


def _segment_card(tl: Timeline, seg: Segment) -> str:
    action_event = seg.action or {}
    action = action_event.get("action") or {}
    detail = action_event.get("detail") or {}
    policy = action_event.get("policy") or next((x.get("policy") for x in seg.events if x.get("policy")), None) or {}
    lease = action_event.get("lease") or {}
    target = seg.target or {}
    observe = next((x for x in seg.events if x["kind"] == "observe"), None)
    usage = (seg.turn or {}).get("usage") or detail.get("usage") or {}
    tokens = None
    if usage.get("input_tokens") or usage.get("output_tokens"):
        tokens = f"{usage.get('input_tokens', 0)} in / {usage.get('output_tokens', 0)} out" + (
            f" · {usage['cache_read_input_tokens']} cached" if usage.get("cache_read_input_tokens") else "")
    policy_txt = (
        f'<span class="pill {"good" if policy.get("allowed") else "bad"}">{"allowed" if policy.get("allowed") else "denied"}</span> '
        f'{e(policy.get("rule"))}: {e(policy.get("reason"))}' if policy else None
    )
    other = [x for x in seg.events if x["kind"] not in ("action", "observe", "state", "policy")]
    facts = _facts([
        ("actor", e(seg.actor)),
        ("action", f"<code>{e(describe_action(action, seg.target))}</code>" if action else None),
        ("rationale", e(action_event.get("rationale")) if action_event.get("rationale") else None),
        ("target", e(f'{target.get("role", "")} "{target.get("name", "")}" ({target.get("ref", "")})') if target else None),
        ("inputs used", e(", ".join(detail.get("tokens") or [])) or None),
        ("effect", f'<span class="pill {"warn" if seg.effect == "commit" else "neutral"}">{e(seg.effect)}</span>' if seg.effect else None),
        ("result", e(f'{detail.get("status")}: {detail.get("message")}') if detail.get("status") else None),
        ("states", e(" → ".join(seg.states)) if seg.states else None),
        ("url", f"<code>{e((observe or {}).get('detail', {}).get('url'))}</code>" if observe else None),
        ("policy", policy_txt),
        ("lease", e(f'{lease.get("holder")} (generation {lease.get("generation")})') if lease else None),
        ("duration", e(_ms(seg.duration_ms))),
        ("model tokens", e(tokens) if tokens else None),
        ("also", e(", ".join(sorted({x["kind"] for x in other}))) if other else None),
    ])
    shots = "".join(
        f'<figure><img class="shot" loading="lazy" src="{src}" alt="{e(rel)}"><figcaption>{e(rel)}</figcaption></figure>'
        for rel in ([seg.screenshot] if seg.screenshot else []) + seg.images
        if (src := _image(tl, rel))
    )
    observation = (seg.turn or {}).get("observation")
    extra = _compiled_block(tl, seg)
    if observation:
        cut = observation[:_OBSERVATION_CHARS]
        more = f"\n… {len(observation) - len(cut)} more characters in transcript.json" if len(observation) > len(cut) else ""
        extra += f"<details><summary>What the model saw</summary><pre>{e(cut + more)}</pre></details>"
    extra += f"<details><summary>Raw events ({len(seg.events)})</summary>{_pre_json(seg.events)}</details>"
    notable = seg.status != "ok" or seg.effect == "commit" or seg.images
    title = describe_action(action, seg.target) or seg.label
    return (
        f'<details class="card st-{e(seg.status)}" id="{e(_anchor(seg.key))}"{" open" if notable else ""}>'
        f'<summary><span class="dot actor-{e(seg.actor)}"></span><b>{e(seg.label)}</b> <span class="sub">{e(title)}</span>'
        f'<span class="right"><span class="pill {_status_class(seg.status)}">{e(seg.status)}</span> {e(_ms(seg.duration_ms))}</span></summary>'
        f'<div class="card-body"><div>{facts}{extra}</div><div class="shots">{shots}</div></div></details>'
    )


# ---------------------------------------------------------------------------------------------- trace -> capability

_ROLE = {
    "compiled": "compiled from this turn",
    "check_before": "identity check added before the commit",
    "check_after": "identity check added: the value was seen after this turn",
}


def _effect_note(lineage: Lineage, step_id: str) -> str:
    change = lineage.effect_changes.get(step_id)
    if not change:
        return ""
    recorded, compiled = change
    why = (" because the click only opens the confirmation prompt; the click that confirms is the commit"
           if (recorded, compiled) == ("commit", "read") else "")
    return f"recorded as {recorded}, compiled as {compiled}{why}"


def _target_html(lineage: Lineage, target_id: str | None) -> str:
    target = lineage.targets.get(target_id or "")
    if not target:
        return ""
    items = "".join(
        f'<li class="{"primary" if i == 0 else ""}"><code>{e(describe_strategy(st))}</code> <span class="sub">'
        f'{e(st.get("why"))}{" · " + e(st.get("matched_at_record")) if st.get("matched_at_record") else ""}</span></li>'
        for i, st in enumerate(target.get("strategies") or [])
    )
    return (f'<div class="target"><span class="sub">target</span> <code>{e(target_id)}</code> '
            f'<span class="sub">{e(target.get("description"))}, locators in the order replay tries them:</span>'
            f'<ol class="strategies">{items}</ol></div>')


def _step_html(lineage: Lineage, link: Link, *, detail: bool) -> str:
    step = link.step
    commit = ' <span class="mark commit">commit</span>' if step.get("effect") == "commit" else ""
    head = (f'<div class="cstep {e(link.role)}"><code class="sid">{e(step["id"])}</code> '
            f'<code>{e(describe_step(step))}</code>{commit}')
    if not detail:
        return head + "</div>"
    if link.role != "compiled":  # identity checks: one line with why they are there
        return head + f'<div class="sub">{e(_ROLE[link.role])}: {e(step.get("intent"))}</div></div>'
    note = _effect_note(lineage, step["id"])
    verify = describe_predicate(step.get("verify"))
    facts = _facts([
        ("from", e(_ROLE.get(link.role, link.role))),
        ("intent", e(step.get("intent"))),
        ("expects state", f"<code>{e(step.get('expect'))}</code>"),
        ("effect", e(step.get("effect")) + (f' <span class="sub">({e(note)})</span>' if note else "")),
        ("verifies after", f"<code>{e(verify)}</code>" if verify else None),
        ("timeout", e(_ms(step.get("timeout_ms")))),
    ])
    return head + facts + _target_html(lineage, (step.get("do") or {}).get("target")) + "</div>"


def _compiled_block(tl: Timeline, seg: Segment) -> str:
    lineage, turn = tl.lineage, turn_number(seg.step_id)
    if lineage is None or turn is None:
        return ""
    links = lineage.by_turn.get(turn, [])
    if links:
        body = "".join(_step_html(lineage, link, detail=True) for link in links)
        return (f'<details class="compiled" open><summary>Compiled to {e(", ".join(lineage.step_ids(turn)))}</summary>'
                f'{body}</details>')
    why = lineage.dropped.get(turn)
    if why and why.startswith("done"):
        succ = lineage.success
        checks = ", ".join(describe_predicate(c) for c in succ.get("checks", []))
        return (f'<div class="compiled-note">Becomes the success checkpoint: state <code>{e(succ.get("state"))}</code>, '
                f'outputs {e(", ".join(succ.get("outputs", [])))}, checks <code>{e(checks)}</code></div>')
    return f'<div class="compiled-note dropped">Not compiled: {e(why)}</div>' if why else ""


def _lineage_section(tl: Timeline) -> str:
    lineage = tl.lineage
    if lineage is None:
        return ""
    c = lineage.counts()
    prov = lineage.capability.get("provenance") or {}
    by_turn = {turn_number(s.step_id): s for s in tl.segments if turn_number(s.step_id) is not None}
    rows = []
    for turn in sorted({*lineage.by_turn, *lineage.dropped}):
        seg = by_turn.get(turn)
        action = seg.action if seg else None
        left = describe_action(action.get("action") if action else None, seg.target if seg else None)
        links = lineage.by_turn.get(turn, [])
        if links:
            right = "".join(_step_html(lineage, link, detail=False) for link in links)
            notes = [n for n in (_effect_note(lineage, x.step["id"]) for x in links) if n]
        else:
            why = lineage.dropped.get(turn, "")
            right = f'<span class="sub">{"success checkpoint" if why.startswith("done") else "dropped"}</span>'
            notes = [why]
        href = f"#{_anchor(seg.key)}" if seg else "#"
        rows.append(
            f'<tr class="{"" if links else "dropped"}"><td><a href="{e(href)}"><b>t{turn:02d}</b></a></td>'
            f'<td><code>{e(left)}</code></td><td class="arrow">→</td><td>{right}</td>'
            f'<td class="sub">{"<br>".join(e(n) for n in notes)}</td></tr>'
        )
    for step in lineage.unmatched:
        rows.append(f'<tr class="dropped"><td>?</td><td class="sub">no turn matches</td><td class="arrow">→</td>'
                    f'<td><code>{e(step.get("id"))}</code> <code>{e(describe_step(step))}</code></td>'
                    f'<td class="sub">edited by hand, or compiled by a different compiler</td></tr>')
    succ = lineage.success
    summary = (f'{c["turns"]} model turns became {c["steps"]} replay steps: {c["compiled"]} compiled from actions, '
               f'{c["checks"]} identity checks added, {c["dropped"]} failed attempts dropped, and <code>done</code> '
               f'became the success checkpoint (state <code>{e(succ.get("state"))}</code>).')
    warn = f'<div class="callout warn">{c["unmatched"]} step(s) of the draft match no turn.</div>' if c["unmatched"] else ""
    return (f'<section><h2>Discovery → capability</h2><p>{summary}</p>'
            f'<p class="sub">Draft <code>{e(lineage.capability.get("id"))}@{e(lineage.capability.get("version"))}</code> '
            f'(capability.draft.json), compiled by <code>{e(prov.get("compiler"))}</code> from trace '
            f'<code>{e(str(prov.get("trace_sha256", ""))[:12])}</code>. Replay runs these steps with no model.</p>{warn}'
            f'<div class="scroll"><table class="list lineage"><tr><th>turn</th><th>what the model did</th><th></th>'
            f'<th>replay step</th><th>note</th></tr>{"".join(rows)}</table></div></section>')


def _handoff_card(tl: Timeline, h: Handoff) -> str:
    req = h.request or {}
    decisions = "".join(
        f"<li><code>{e(d.get('action'))}</code> by {e(d.get('operator'))}"
        f"{' → ' + e(d['verdict']) if d.get('verdict') else ''}{' (' + e(d['reason']) + ')' if d.get('reason') and d.get('reason') != 'ok' else ''}"
        f" <span class=\"sub\">+{e(_ms(int((d['ts'] - h.start).total_seconds() * 1000)))}</span></li>"
        for d in h.decisions
    )
    actions = "".join(
        f"<tr><td>+{e(_ms(int((a['ts'] - h.start).total_seconds() * 1000)))}</td><td><code>{e(describe_action(a))}</code></td>"
        f"<td>{e(a.get('path'))}</td></tr>"
        for a in h.human_actions
    )
    facts = _facts([
        ("reason", f'<span class="pill warn">{e(h.reason)}</span>'),
        ("step", e(h.step_id)),
        ("intervention", f"<code>{e(h.intervention)}</code>" if h.intervention else None),
        ("detail", e(req.get("detail"))),
        ("page state", e(f'{req.get("classified_state")} (expected {req.get("expected_state")})') if req.get("classified_state") else None),
        ("commit attempted", e(req.get("commit_attempted")) if "commit_attempted" in req else None),
        ("SLA deadline", e(h.sla_deadline)),
        ("operator", e(h.operator)),
        ("waited for a human", e(_ms(h.wait_ms))),
        ("total", e(_ms(h.duration_ms))),
        ("lease generations", e(" → ".join(str(g) for g in h.generations))),
        ("ended", f'<span class="pill {_status_class(h.outcome)}">{e(h.outcome)}</span>' + (f" verdict <b>{e(h.verdict)}</b>" if h.verdict else "")),
        ("decisions", f"<ul>{decisions}</ul>" if decisions else None),
    ])
    table = (f'<details open><summary>Captured human actions ({len(h.human_actions)}, values redacted)</summary>'
             f'<table class="list"><tr><th>at</th><th>action</th><th>page</th></tr>{actions}</table></details>' if actions else "")
    img = _image(tl, req.get("screenshot"))
    shot = f'<figure><img class="shot" src="{img}" alt="handoff screenshot"><figcaption>{e(req.get("screenshot"))}: what the operator was shown</figcaption></figure>' if img else ""
    return (
        f'<details class="card handoff-card" open id="{e(_anchor("handoff-" + (h.intervention or str(id(h)))))}">'
        f'<summary><span class="dot actor-human"></span><b>handoff</b> <span class="sub">{e(h.reason)} · {e(h.operator or "unclaimed")}</span>'
        f'<span class="right">{e(_ms(h.duration_ms))}</span></summary>'
        f'<div class="card-body"><div>{facts}{table}</div><div class="shots">{shot}</div></div></details>'
    )


def _network(tl: Timeline) -> str:
    if not tl.network:
        return ""
    rows = "".join(
        f'<tr class="{"bad" if not (x.get("policy") or {}).get("allowed") else ""}"><td>+{e(_ms(int((x["_ts"] - tl.start).total_seconds() * 1000)))}</td>'
        f'<td>{e((x.get("detail") or {}).get("method"))}</td><td>{e((x.get("detail") or {}).get("resource_type"))}</td>'
        f'<td><code>{e((x.get("detail") or {}).get("url"))}</code></td>'
        f'<td>{"allowed" if (x.get("policy") or {}).get("allowed") else "blocked"}</td><td>{e((x.get("policy") or {}).get("reason"))}</td></tr>'
        for x in tl.network
    )
    return (f'<details class="section"><summary><h2>Request policy ({len(tl.network)} checks, {len(tl.denied)} blocked)</h2></summary>'
            f'<p class="sub">Every request the page made goes through the policy guard. Blocked sub-resources (stylesheets, '
            f'trackers) are the allowlist working, not failures of the run.</p>'
            f'<table class="list"><tr><th>at</th><th>method</th><th>type</th><th>url</th><th></th><th>reason</th></tr>{rows}</table></details>')


def _manifest(tl: Timeline) -> str:
    m = tl.manifest
    red = m.get("redaction") or {}
    files = "".join(f"<tr><td><code>{e(f.get('path'))}</code></td><td>{e(f.get('bytes'))}</td><td><code>{e(str(f.get('sha256', ''))[:16])}</code></td></tr>"
                    for f in m.get("files", []))
    return (f'<details class="section"><summary><h2>Bundle</h2></summary>'
            f'{_facts([("status", e(m.get("status"))), ("events", e(m.get("events"))), ("created", e(m.get("created"))), ("closed", e(m.get("updated"))), ("redaction", e(f"{red.get("known_values", 0)} known values; patterns: {", ".join(red.get("patterns", []))}") if red else None)])}'
            f'<table class="list"><tr><th>file</th><th>bytes</th><th>sha256</th></tr>{files}</table></details>')


# ------------------------------------------------------------------------------------------------------ page


def render(tl: Timeline) -> str:
    r = tl.result
    failure = tl.failure
    side = r.get("side_effects") or {}
    outputs = ", ".join(f"{k} = {v}" for k, v in tl.outputs.items())
    usage = tl.usage
    stats = [
        ("duration", _ms(tl.duration_ms)),
        ("steps", str(sum(1 for s in tl.segments if s.step_id))),
        ("handoffs", str(len(tl.handoffs))),
        ("human wait", _ms(sum(h.wait_ms or 0 for h in tl.handoffs)) if tl.handoffs else None),
        ("model tokens", f"{usage.get('input_tokens', 0):,} in / {usage.get('output_tokens', 0):,} out" if tl.kind == "discovery" else "none (replay)"),
        ("commit", side.get("commit_state")),
        ("blocked requests", str(len(tl.denied))),
    ]
    stat_html = "".join(f'<div class="stat"><div class="k">{e(k)}</div><div class="v">{e(v)}</div></div>' for k, v in stats if v)
    fail_html = ""
    if failure:
        fail_html = (f'<div class="callout bad"><b>{e(failure.get("code"))}</b> at <code>{e(failure.get("step_id"))}</code>: '
                     f'{e(failure.get("detail"))}<div class="sub">{e(failure.get("step_intent"))}'
                     f'{" · retry safe" if r.get("retry_safe") else " · not safe to retry" if "retry_safe" in r else ""}</div></div>')
    gt = tl.ground_truth or {}
    gt_html = ""
    if gt:
        gt_html = (f'<div class="callout neutral"><b>Ground truth</b> (REST side channel the replay never uses): expected '
                   f'<code>{e((gt.get("expected") or {}).get("status"))}/{e((gt.get("expected") or {}).get("outcome"))}</code>, '
                   f'REST state {"unchanged" if gt.get("rest_state_unchanged") else "changed"}'
                   f'{", new payments: " + e(gt["new_payments"]) if "new_payments" in gt else ""}'
                   f'{", mismatches: " + e("; ".join(gt["mismatches"])) if gt.get("mismatches") else ", no mismatches"}.</div>')
    cards = []
    for kind, item in _rows(tl):
        cards.append(_segment_card(tl, item) if kind == "seg" else _handoff_card(tl, item))  # type: ignore[arg-type]
    title = f"{tl.kind.capitalize()} · {tl.run_id}"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title><style>{_CSS}</style></head><body>
<header>
  <div class="eyebrow"><span class="pill {'agent' if tl.kind == 'discovery' else 'replay'}">{e(tl.kind)}</span>
    <code>{e(tl.run_id)}</code>{' · ' + e(tl.capability) if tl.capability else ''}{' · model ' + e(tl.model) if tl.model else ''}
    · {e(tl.start.strftime('%Y-%m-%d %H:%M:%S UTC'))}</div>
  <h1>{e(tl.title)}</h1>
  <div class="outcome"><span class="pill big {_status_class(tl.status)}">{e(tl.status)}</span>
    {f'<span class="pill big {_status_class(tl.outcome)}">{e(tl.outcome)}</span>' if tl.outcome and tl.outcome != tl.status else ''}
    {f'<span class="outputs">{e(outputs)}</span>' if outputs else ''}</div>
  <div class="stats">{stat_html}</div>
  {fail_html}{gt_html}
</header>
<main>
<section><h2>Timeline</h2>{_waterfall(tl)}</section>
{_lineage_section(tl)}
<section><h2>Steps and handoffs <button type="button" id="toggle">expand all</button></h2>{''.join(cards)}</section>
{_network(tl)}
{_manifest(tl)}
</main>
<footer>Rendered by <code>cua view</code> from the redacted evidence bundle <code>{e(tl.root.name)}</code>; nothing on this page came from the live run.</footer>
<div id="zoom" hidden><img alt=""></div>
<script>{_JS}</script>
</body></html>
"""


def render_index(pages: list[tuple[str, Timeline]]) -> str:
    rows = "".join(
        f'<tr><td><a href="{e(href)}">{e(tl.root.name)}</a></td><td><span class="pill {"agent" if tl.kind == "discovery" else "replay"}">{e(tl.kind)}</span></td>'
        f'<td><span class="pill {_status_class(tl.status)}">{e(tl.status)}</span> {e(tl.outcome if tl.outcome != tl.status else "")}</td>'
        f'<td>{e(_ms(tl.duration_ms))}</td><td>{len(tl.handoffs)}</td><td class="sub">{e(tl.title)}</td></tr>'
        for href, tl in pages
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Evidence traces</title><style>{_CSS}</style></head><body>
<header><h1>Evidence traces</h1><p class="sub">{len(pages)} run bundles. Each page is self-contained.</p></header>
<main><div class="scroll"><table class="list index"><tr><th>bundle</th><th>kind</th><th>result</th><th>duration</th><th>handoffs</th><th>goal / capability</th></tr>{rows}</table></div></main>
</body></html>
"""


# --------------------------------------------------------------------------------------------------- command


def bundles_under(path: Path) -> list[Path]:
    if (path / "events.jsonl").is_file():
        return [path]
    return sorted(p.parent for p in path.glob("*/events.jsonl"))


def cmd_view(args: argparse.Namespace) -> int:
    from cua.config import REPO_ROOT

    src = Path(args.bundle)
    bundles = bundles_under(src)
    if not bundles:
        print(f"error: {src} is not a run bundle and holds none", file=sys.stderr)
        return 2
    single = len(bundles) == 1 and bundles[0] == src
    if args.out:
        out = Path(args.out)
    else:
        out = REPO_ROOT / "runs" / "views" / (f"{src.resolve().name}.html" if single else "")
    out_dir = out.parent if single else out
    out_dir.mkdir(parents=True, exist_ok=True)
    pages: list[tuple[str, Timeline]] = []
    status = 0
    for b in bundles:
        try:
            tl = load(b)
        except TimelineError as err:
            print(f"skip {b}: {err}", file=sys.stderr)
            status = 1
            continue
        target = out if single else out_dir / f"{b.name}.html"
        target.write_text(render(tl), encoding="utf-8", newline="\n")
        pages.append((target.name, tl))
        print(f"wrote {target}")
    first = out if single else out_dir / "index.html"
    if not single:
        first.write_text(render_index(pages), encoding="utf-8", newline="\n")
        print(f"wrote {first}")
    if args.open and pages:
        webbrowser.open(first.resolve().as_uri())
    return status


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("view", help="render a run bundle (or a folder of them) as a self-contained HTML trace")
    p.add_argument("bundle", help="a run bundle folder (evidence/<name> or runs/<run_id>), or a folder of bundles")
    p.add_argument("--out", default=None,
                   help="output .html for one bundle, or output folder for several (default: runs/views/)")
    p.add_argument("--open", action="store_true", help="open the page in the default browser")
    p.set_defaults(func=cmd_view)


_CSS = """
:root{--bg:#f7f7f5;--panel:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e4e3de;--track:#efeee9;
--agent:#7c5cd6;--replay:#2f6fd0;--human:#d98a00;--system:#9a9a93;--good:#1f8a4c;--warn:#b86e00;--bad:#c63b32;
--good-bg:#e5f4ea;--warn-bg:#fbf0dc;--bad-bg:#fbe6e4;--neutral-bg:#efeee9;--code:#f1f0ec}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--panel:#1f1f1d;--ink:#ecebe6;--muted:#9c9b94;--line:#33332f;--track:#2a2a27;
--agent:#a48af0;--replay:#6aa0f0;--human:#f0a830;--system:#77776f;--good:#5cc489;--warn:#f0a830;--bad:#f07a70;
--good-bg:#1d3326;--warn-bg:#3a2c12;--bad-bg:#3d1f1c;--neutral-bg:#2a2a27;--code:#2a2a27}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
header,main,footer{max-width:1200px;margin:0 auto;padding:16px}header{padding-top:28px}
h1{font-size:22px;margin:6px 0 10px;font-weight:600}h2{font-size:15px;margin:22px 0 10px;display:inline-flex;gap:10px;align-items:center}
code,pre{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12.5px}code{background:var(--code);padding:1px 5px;border-radius:4px;word-break:break-all}
pre{background:var(--code);padding:10px;border-radius:6px;overflow:auto;max-height:420px;white-space:pre-wrap;word-break:break-word}
.sub{color:var(--muted);font-size:12.5px}.eyebrow{color:var(--muted);font-size:12.5px}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600;background:var(--neutral-bg)}
.pill.big{font-size:13px;padding:3px 10px}.good{background:var(--good-bg);color:var(--good)}.warn{background:var(--warn-bg);color:var(--warn)}
.bad{background:var(--bad-bg);color:var(--bad)}.neutral{background:var(--neutral-bg)}.pill.agent{background:var(--agent);color:#fff}.pill.replay{background:var(--replay);color:#fff}
.outcome{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.outputs{font-family:ui-monospace,Consolas,monospace;font-size:13px}
.stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px;margin:14px 0}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px 10px}.stat .k{color:var(--muted);font-size:12px}.stat .v{font-weight:600}
.callout{border-radius:8px;padding:10px 12px;margin:8px 0;border:1px solid var(--line)}
.legend{display:flex;flex-wrap:wrap;gap:12px;font-size:12px;color:var(--muted);margin-bottom:8px}.legend span{display:inline-flex;gap:5px;align-items:center}
.sw{display:inline-block;width:14px;height:10px;border-radius:3px}
.wf{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:6px 0}
.wf-row{display:grid;grid-template-columns:minmax(150px,32%) 1fr;align-items:center;padding:3px 12px;color:inherit;text-decoration:none;min-height:30px}
a.wf-row:hover{background:var(--track)}.wf-label{overflow:hidden;white-space:nowrap;text-overflow:ellipsis;padding-right:10px}
.wf-label .sub{margin-left:8px}.wf-track{position:relative;height:18px;background:var(--track);border-radius:4px}
.wf-axis .wf-track{background:none;height:16px}.tick{position:absolute;transform:translateX(-50%);font-size:11px;color:var(--muted);white-space:nowrap}
.tick:first-child{transform:none}.tick:last-child{transform:translateX(-100%)}
.bar{position:absolute;top:2px;bottom:2px;border-radius:3px;min-width:3px}.dur{position:absolute;top:0;font-size:11px;color:var(--muted);white-space:nowrap}
.actor-agent{background:var(--agent)}.actor-replay{background:var(--replay)}.actor-human{background:var(--human)}.actor-system{background:var(--system)}
.handoff{background:var(--human)}.handoff-wait{background:repeating-linear-gradient(45deg,var(--human) 0 3px,transparent 3px 7px);box-shadow:inset 0 0 0 1px var(--human)}
.bar.st-error,.bar.st-denied,.sw.st-error{outline:2px solid var(--bad);outline-offset:1px}.bar.st-escalated,.sw.st-escalated{outline:2px solid var(--warn);outline-offset:1px}
.sw.st-error,.sw.st-escalated{background:transparent}
.mark{font-size:11px;padding:0 6px;border-radius:4px;margin-left:6px}.mark.commit{background:var(--warn-bg);color:var(--warn)}.mark.human{background:var(--warn-bg);color:var(--warn)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;margin:8px 0}.card>summary{cursor:pointer;padding:10px 12px;display:flex;gap:8px;align-items:center;list-style:none}
.card>summary::-webkit-details-marker{display:none}.card.st-error,.card.st-denied{border-left:3px solid var(--bad)}.card.st-escalated,.handoff-card{border-left:3px solid var(--warn)}
.card>summary .sub{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.right{margin-left:auto;display:flex;gap:8px;align-items:center;white-space:nowrap;color:var(--muted);font-size:12.5px}
.dot{width:10px;height:10px;border-radius:50%;flex:none}
.card-body{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,46%);gap:16px;padding:0 12px 12px}
.facts{border-collapse:collapse;width:100%}.facts th{text-align:left;color:var(--muted);font-weight:500;padding:3px 10px 3px 0;vertical-align:top;white-space:nowrap;width:1%}
.facts td{padding:3px 0;word-break:break-word}.facts ul{margin:0;padding-left:18px}
.shots figure{margin:0 0 10px}.shot{width:100%;border:1px solid var(--line);border-radius:6px;cursor:zoom-in;display:block}figcaption{font-size:11.5px;color:var(--muted)}
details>summary{cursor:pointer}details details{margin-top:8px}details details>summary{color:var(--muted);font-size:12.5px}
.section>summary{list-style:none}.section>summary::-webkit-details-marker{display:none}.section>summary h2::before{content:"▸ ";color:var(--muted)}.section[open]>summary h2::before{content:"▾ "}
.list{border-collapse:collapse;width:100%;font-size:12.5px;background:var(--panel)}.list th,.list td{text-align:left;padding:4px 8px;border-bottom:1px solid var(--line);vertical-align:top}
.list td code{word-break:break-all}.list tr.bad td{background:none;color:inherit}.list tr.bad td:nth-child(5){color:var(--bad)}
.scroll{overflow-x:auto}button{font:inherit;font-size:12px;border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:6px;padding:2px 8px;cursor:pointer}
.compiled{border:1px solid var(--line);border-radius:6px;padding:6px 10px;margin-top:10px;background:var(--bg)}
.compiled>summary{color:var(--ink);font-weight:600;font-size:13px}.compiled .facts{margin:4px 0 2px}
.cstep{padding:6px 0;border-top:1px dashed var(--line)}.compiled .cstep:first-of-type{border-top:0}
.cstep .sid{background:var(--replay);color:#fff;white-space:nowrap}.cstep.check_before .sid,.cstep.check_after .sid{background:var(--system);color:#fff}
.lineage .cstep{padding:1px 0;border:0}.lineage td.arrow{color:var(--muted)}.lineage tr.dropped td{color:var(--muted)}
.target{margin-top:4px}.strategies{margin:2px 0 0;padding-left:22px;font-size:12.5px}.strategies li.primary code{outline:1px solid var(--replay)}
.compiled-note{margin-top:10px;font-size:13px;padding:6px 10px;border-radius:6px;background:var(--neutral-bg)}.compiled-note.dropped{color:var(--muted)}
footer{color:var(--muted);font-size:12px;padding-bottom:40px}
#zoom{position:fixed;inset:0;background:rgba(0,0,0,.82);display:flex;align-items:center;justify-content:center;z-index:9;cursor:zoom-out;padding:16px}
#zoom[hidden]{display:none}#zoom img{max-width:100%;max-height:100%;border-radius:6px}
@media (max-width:760px){.lineage tr:first-child,.lineage td.arrow{display:none}.lineage tr{display:block;padding:6px 0;border-bottom:1px solid var(--line)}
.lineage td{display:block;border:0;padding:2px 8px}.card-body{grid-template-columns:1fr}.wf-row{grid-template-columns:1fr}.wf-label{padding-bottom:2px}.dur{display:none}}
"""

_JS = """
const zoom=document.getElementById('zoom'),zimg=zoom.querySelector('img');
document.addEventListener('click',ev=>{const t=ev.target;if(t.classList&&t.classList.contains('shot')){zimg.src=t.src;zoom.hidden=false}});
zoom.addEventListener('click',()=>{zoom.hidden=true;zimg.removeAttribute('src')});
document.addEventListener('keydown',ev=>{if(ev.key==='Escape')zoom.hidden=true});
const tg=document.getElementById('toggle');
if(tg)tg.addEventListener('click',()=>{const cards=[...document.querySelectorAll('.card')];const open=cards.some(c=>!c.open);cards.forEach(c=>c.open=open);tg.textContent=open?'collapse all':'expand all'});
document.querySelectorAll('a.wf-row').forEach(a=>a.addEventListener('click',()=>{const d=document.querySelector(a.getAttribute('href'));if(d&&d.tagName==='DETAILS')d.open=true}));
"""
