"""`cua export-otel`: send a run bundle to an OpenTelemetry backend as one trace (OTLP/HTTP JSON).

    cua export-otel evidence/replay_commit_unknown --out trace.otlp.json        write the payload, send nothing
    cua export-otel evidence/ --endpoint http://localhost:4318                  Jaeger / any OTLP collector
    cua export-otel runs/<run_id> --endpoint https://<langfuse>/api/public/otel --header "Authorization=Basic ..."

Off by default: nothing is exported unless the command is run with `--out` or `--endpoint`. It is a second way out
of the system for run data, so it is fenced the same way as the evidence itself:

- the source is a closed bundle, which the evidence writer already redacted; the exporter never sees a live run;
- the bundle and then the exact payload are checked by the evidence lint (`cua.safety.lint`: configured secrets,
  SSNs, cookies, e-mail addresses) and any finding stops the export;
- screenshots, DOM snapshots and what the model saw (`transcript.json` observations) are never exported; spans carry
  the action, the model's stated rationale, policy, lease and timing.

Mapping (deterministic ids, so exporting the same bundle twice replaces rather than duplicates in most backends):

    trace                 one per run (trace id from the run id)
    root span             the run: kind, capability, status/outcome, model and token usage (GenAI semantic
                          conventions), blocked requests as span events
    step spans            one per timeline segment: action, rationale, effect, policy, lease; ERROR when the step
                          errored, was denied or escalated; its evidence events as span events; on discovery, the
                          replay steps it compiled to (`cua.compiled_to`) or why it was not compiled
    handoff spans         one per human handoff, with a child span for the wait before a human claimed it; the
                          operator's decisions and captured actions (kind and field label only) as span events

Headers: `--header K=V` (repeatable) and the standard `OTEL_EXPORTER_OTLP_HEADERS` (`k1=v1,k2=v2`). Header values are
never printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from cua.evidence.timeline import Handoff, Segment, Timeline, TimelineError, describe_action, load, turn_number

SCOPE = {"name": "cua.evidence", "version": "1"}
_INTERNAL = 1
_UNSET, _OK, _ERROR = 0, 1, 2
_MAX_STR = 2000


class ExportError(Exception):
    """The export was refused or failed; the message carries no payload content or header values."""


def _ns(ts: datetime) -> str:
    return str(int(ts.timestamp() * 1_000_000) * 1000)


def _id(*parts: str, size: int) -> str:
    return hashlib.sha256("\x1f".join(("cua",) + parts).encode("utf-8")).hexdigest()[: size * 2]


def _value(v: Any) -> dict[str, Any] | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, (list, tuple)):
        values = [x for x in (_value(i) for i in v) if x is not None]
        return {"arrayValue": {"values": values}}
    return {"stringValue": str(v)[:_MAX_STR]}


def _attrs(pairs: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for k, v in pairs.items():
        if v in (None, "", []):
            continue
        if (value := _value(v)) is not None:
            out.append({"key": k, "value": value})
    return out


def _span(trace_id: str, span_id: str, parent: str | None, name: str, start: datetime, end: datetime,
          attributes: dict[str, Any], events: list[dict[str, Any]] | None = None, status: tuple[int, str] = (_UNSET, "")
          ) -> dict[str, Any]:
    span: dict[str, Any] = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": name[:200],
        "kind": _INTERNAL,
        "startTimeUnixNano": _ns(start),
        "endTimeUnixNano": _ns(max(end, start)),
        "attributes": _attrs(attributes),
        "events": events or [],
        "status": {"code": status[0], **({"message": status[1]} if status[1] else {})},
    }
    if parent:
        span["parentSpanId"] = parent
    return span


def _event(name: str, ts: datetime, attributes: dict[str, Any]) -> dict[str, Any]:
    return {"timeUnixNano": _ns(ts), "name": name, "attributes": _attrs(attributes)}


def _flat(prefix: str, d: dict[str, Any] | None) -> dict[str, Any]:
    """Scalar fields of a dict, one level deep (nested structures are left to the bundle)."""
    return {f"{prefix}.{k}": v for k, v in (d or {}).items() if isinstance(v, (str, int, float, bool))}


def _evidence_event(x: dict[str, Any]) -> dict[str, Any]:
    policy = x.get("policy") or {}
    lease = x.get("lease") or {}
    detail = {k: v for k, v in (x.get("detail") or {}).items() if k not in ("usage",)}
    return _event(x["kind"], x["_ts"], {
        "cua.seq": x.get("seq"),
        "cua.actor": x.get("actor"),
        "cua.state": x.get("state"),
        "cua.action": describe_action(x.get("action")) or None,
        "cua.policy.allowed": policy.get("allowed"),
        "cua.policy.rule": policy.get("rule"),
        "cua.lease.holder": lease.get("holder"),
        "cua.lease.generation": lease.get("generation"),
        "cua.duration_ms": x.get("duration_ms"),
        **_flat("cua.detail", detail),
    })


def _segment_span(tl: Timeline, trace_id: str, root_id: str, seg: Segment) -> dict[str, Any]:
    action_event = seg.action or {}
    action = action_event.get("action")
    detail = action_event.get("detail") or {}
    policy = action_event.get("policy") or {}
    lease = action_event.get("lease") or {}
    usage = (seg.turn or {}).get("usage") or detail.get("usage") or {}
    summary = describe_action(action, seg.target)
    status = {
        "error": (_ERROR, detail.get("message") or "step error"),
        "denied": (_ERROR, "denied by policy"),
        "escalated": (_ERROR, "escalated to a human"),
    }.get(seg.status, (_OK, ""))
    replay = seg.actor == "replay"
    turn = turn_number(seg.step_id)
    compiled = tl.lineage.step_ids(turn) if tl.lineage and turn is not None else []
    dropped = tl.lineage.dropped.get(turn) if tl.lineage and turn is not None else None
    return _span(
        trace_id, _id(tl.run_id, "seg", seg.key, size=8), root_id,
        f"{seg.label}: {summary}" if seg.step_id and summary else seg.label,
        seg.start, seg.end,
        {
            "cua.step_id": seg.step_id,
            "cua.actor": seg.actor,
            "cua.action": summary or None,
            "cua.action.kind": (action or {}).get("kind"),
            "cua.rationale": action_event.get("rationale"),
            "cua.effect": seg.effect,
            "cua.commit_attempted": seg.commit_attempted or None,
            "cua.states": seg.states or None,
            "cua.inputs_used": detail.get("tokens") or None,
            "cua.result": detail.get("status"),
            "cua.policy.allowed": policy.get("allowed"),
            "cua.policy.rule": policy.get("rule"),
            "cua.lease.holder": lease.get("holder"),
            "cua.lease.generation": lease.get("generation"),
            "cua.compiled_to": compiled or None,
            "cua.not_compiled": dropped,
            "gen_ai.usage.input_tokens": usage.get("input_tokens") if not replay and usage else None,
            "gen_ai.usage.output_tokens": usage.get("output_tokens") if not replay and usage else None,
            "openinference.span.kind": "TOOL" if replay else "AGENT",
        },
        [_evidence_event(x) for x in seg.events],
        status,
    )


def _handoff_spans(tl: Timeline, trace_id: str, root_id: str, n: int, h: Handoff) -> list[dict[str, Any]]:
    key = h.intervention or f"handoff-{n}"
    span_id = _id(tl.run_id, "handoff", key, size=8)
    events = [_event("request_human", h.start, {"cua.reason": h.reason, "cua.sla_deadline": h.sla_deadline})]
    if h.claimed is not None:
        events.append(_event("claim", h.claimed, {"cua.operator": h.operator}))
    events += [
        _event(f"human.{a.get('kind', 'action')}", a["ts"], {"cua.field": a.get("label") or a.get("name"),
                                                               "cua.page": a.get("path")})
        for a in h.human_actions
    ]
    events += [
        _event(f"decision.{d.get('action')}", d["ts"], {"cua.operator": d.get("operator"), "cua.verdict": d.get("verdict"),
                                                         "cua.ok": d.get("ok"), "cua.reason": d.get("reason")})
        for d in h.decisions
    ]
    spans = [_span(
        trace_id, span_id, root_id, f"handoff {h.reason}", h.start, h.end or h.start,
        {
            "cua.handoff.reason": h.reason,
            "cua.step_id": h.step_id,
            "cua.intervention": h.intervention,
            "cua.operator": h.operator,
            "cua.handoff.outcome": h.outcome,
            "cua.handoff.verdict": h.verdict,
            "cua.handoff.wait_ms": h.wait_ms,
            "cua.handoff.human_actions": len(h.human_actions),
            "cua.lease.generations": h.generations or None,
            "cua.commit_attempted": (h.request or {}).get("commit_attempted"),
            "openinference.span.kind": "CHAIN",
        },
        sorted(events, key=lambda ev: int(ev["timeUnixNano"])),
        (_OK, "") if h.end else (_UNSET, ""),
    )]
    if h.claimed is not None:
        spans.append(_span(trace_id, _id(tl.run_id, "wait", key, size=8), span_id, "waiting for a human",
                           h.start, h.claimed, {"cua.handoff.reason": h.reason}))
    return spans


def build(tl: Timeline) -> dict[str, Any]:
    """The OTLP/HTTP JSON payload (`ExportTraceServiceRequest`) for one run."""
    trace_id = _id(tl.run_id, "trace", size=16)
    root_id = _id(tl.run_id, "root", size=8)
    failure = tl.failure or {}
    usage = tl.usage
    if tl.status in ("failure", "error"):
        status = (_ERROR, failure.get("code") or tl.outcome or "failure")
    elif tl.status in ("success", "done"):
        status = (_OK, "")
    else:  # indeterminate: not an error of the system, but not a success either
        status = (_UNSET, tl.outcome or "")
    root = _span(
        trace_id, root_id, None, f"cua.{tl.kind} {tl.capability or tl.run_id}", tl.start, tl.end,
        {
            "cua.run_id": tl.run_id,
            "cua.kind": tl.kind,
            "cua.capability": tl.capability,
            "cua.version": tl.result.get("version"),
            "cua.lock_hash": tl.result.get("lock_hash"),
            "cua.status": tl.status,
            "cua.outcome": tl.outcome,
            "cua.goal": tl.title if tl.kind == "discovery" else None,
            "cua.outputs": [f"{k}={v}" for k, v in tl.outputs.items()] or None,
            "cua.failure.code": failure.get("code"),
            "cua.failure.step_id": failure.get("step_id"),
            "cua.retry_safe": tl.result.get("retry_safe"),
            "cua.commit_state": (tl.result.get("side_effects") or {}).get("commit_state"),
            "cua.handoffs": len(tl.handoffs),
            "cua.requests_blocked": len(tl.denied),
            "cua.compiled.capability": tl.lineage.capability.get("id") if tl.lineage else None,
            "cua.compiled.steps": len(tl.lineage.steps) if tl.lineage else None,
            "gen_ai.operation.name": "invoke_agent" if tl.kind == "discovery" else None,
            "gen_ai.request.model": tl.model if tl.kind == "discovery" else None,
            "gen_ai.usage.input_tokens": usage.get("input_tokens") if tl.kind == "discovery" else None,
            "gen_ai.usage.output_tokens": usage.get("output_tokens") if tl.kind == "discovery" else None,
            "openinference.span.kind": "AGENT" if tl.kind == "discovery" else "CHAIN",
        },
        [
            _event("policy.blocked", x["_ts"], {
                "url.full": (x.get("detail") or {}).get("url"),
                "http.request.method": (x.get("detail") or {}).get("method"),
                "cua.resource_type": (x.get("detail") or {}).get("resource_type"),
                "cua.reason": (x.get("policy") or {}).get("reason"),
            })
            for x in tl.denied
        ],
        status,
    )
    spans = [root]
    spans += [_segment_span(tl, trace_id, root_id, seg) for seg in tl.segments]
    for n, h in enumerate(tl.handoffs):
        spans += _handoff_spans(tl, trace_id, root_id, n, h)
    return {
        "resourceSpans": [{
            "resource": {"attributes": _attrs({"service.name": "cua", "service.namespace": "computer-use",
                                               "cua.app": (tl.trace or {}).get("app")})},
            "scopeSpans": [{"scope": SCOPE, "spans": spans}],
        }]
    }


# ----------------------------------------------------------------------------------------------- guards, send


def check_bundle(tl: Timeline, secrets: list[str]) -> None:
    from cua.safety.lint import lint_tree

    status = tl.manifest.get("status")
    if status in (None, "open"):
        raise ExportError(f"{tl.root.name}: the bundle is not closed (manifest status {status!r})")
    if findings := lint_tree(tl.root, secrets):
        raise ExportError(f"{tl.root.name}: evidence lint findings, nothing exported: " + "; ".join(findings[:10]))


def check_payload(text: str, secrets: list[str]) -> None:
    """The same lint over the exact bytes that would leave."""
    from cua.safety.lint import lint_tree

    with tempfile.TemporaryDirectory() as d:
        Path(d, "payload.json").write_text(text, encoding="utf-8")
        if findings := lint_tree(Path(d), secrets):
            rules = sorted({f.rsplit(": ", 1)[-1] for f in findings})
            raise ExportError(f"the payload trips the evidence lint ({', '.join(rules)}), nothing exported")


def parse_headers(pairs: list[str], env: str | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in [p for p in (env or "").split(",") if p.strip()]:
        if "=" not in item:
            raise ExportError("OTEL_EXPORTER_OTLP_HEADERS entries must be key=value")
        k, v = item.split("=", 1)
        headers[urllib.parse.unquote(k.strip())] = urllib.parse.unquote(v.strip())
    for item in pairs:
        if "=" not in item:
            raise ExportError("--header takes key=value")
        k, v = item.split("=", 1)
        headers[k.strip()] = v.strip()
    return headers


def traces_url(endpoint: str) -> str:
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ExportError("--endpoint must be an http(s) URL")
    return endpoint if endpoint.rstrip("/").endswith("/v1/traces") else endpoint.rstrip("/") + "/v1/traces"


def send(url: str, body: bytes, headers: dict[str, str], timeout: float = 10.0) -> int:
    request = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - scheme checked above
            return response.status
    except urllib.error.HTTPError as err:
        raise ExportError(f"the collector answered HTTP {err.code}") from None
    except (urllib.error.URLError, OSError) as err:
        reason = getattr(err, "reason", err)
        raise ExportError(f"could not reach the collector ({type(reason).__name__})") from None


def cmd_export_otel(args: argparse.Namespace) -> int:
    from cua.config import settings
    from cua.evidence.view import bundles_under

    if bool(args.out) == bool(args.endpoint):
        print("error: give exactly one of --out (write the payload) or --endpoint (send it)", file=sys.stderr)
        return 2
    try:
        url = traces_url(args.endpoint) if args.endpoint else None
        headers = parse_headers(args.header or [], os.environ.get("OTEL_EXPORTER_OTLP_HEADERS")) if url else {}
    except ExportError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    src = Path(args.bundle)
    bundles = bundles_under(src)
    if not bundles:
        print(f"error: {src} is not a run bundle and holds none", file=sys.stderr)
        return 2
    secrets = settings().secret_values()
    payloads: list[dict[str, Any]] = []
    failed = 0
    for b in bundles:
        try:
            tl = load(b)
            check_bundle(tl, secrets)
            payload = build(tl)
            text = json.dumps(payload, ensure_ascii=False)
            check_payload(text, secrets)
            spans = len(payload["resourceSpans"][0]["scopeSpans"][0]["spans"])
            if url:
                code = send(url, text.encode("utf-8"), headers)
                print(f"sent {b.name}: {spans} spans, trace {payload['resourceSpans'][0]['scopeSpans'][0]['spans'][0]['traceId']} "
                      f"-> {urllib.parse.urlparse(url).netloc} (HTTP {code})")
            else:
                payloads.append(payload)
                print(f"built {b.name}: {spans} spans")
        except (TimelineError, ExportError) as err:
            print(f"error: {err}", file=sys.stderr)
            failed += 1
    if args.out and payloads:
        merged = {"resourceSpans": [rs for p in payloads for rs in p["resourceSpans"]]}
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        print(f"wrote {out}")
    return 1 if failed else 0


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "export-otel", help="export a run bundle (or a folder of them) as OpenTelemetry traces (OTLP/HTTP JSON)"
    )
    p.add_argument("bundle", help="a closed run bundle folder, or a folder of bundles")
    p.add_argument("--out", default=None, help="write the OTLP JSON payload to this file and send nothing")
    p.add_argument("--endpoint", default=None,
                   help="OTLP/HTTP endpoint (for example http://localhost:4318); /v1/traces is appended if missing")
    p.add_argument("--header", action="append", metavar="KEY=VALUE",
                   help="extra request header, repeatable (also read from OTEL_EXPORTER_OTLP_HEADERS)")
    p.set_defaults(func=cmd_export_otel)
