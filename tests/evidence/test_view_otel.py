"""`cua view` and `cua export-otel`: the committed bundles read back as timelines, render as escaped self-contained
HTML outside the bundle, and export as OTLP only after the lint passes, without screenshots or page text."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from cua.cli import main
from cua.evidence import otel
from cua.evidence.timeline import load
from cua.evidence.view import render
from cua.evidence.writer import RunBundleWriter
from cua.safety.redact import Redactor

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "evidence"


def _tree_hashes(folder: Path) -> dict[str, str]:
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


def _bundle(tmp_path: Path, rationale: str = "Open the card.", *, close: bool = True) -> Path:
    w = RunBundleWriter("run-x", root=tmp_path, redactor=Redactor())
    w.emit("state", detail={"session": "open"})
    w.emit("observe", actor="agent", step_id="t01", detail={"url": "http://app/x"})
    w.emit("action", actor="agent", step_id="t01", action={"kind": "click", "ref": "e1"}, rationale=rationale,
           detail={"status": "performed", "effect": "read"}, duration_ms=5)
    w.json("result.json", {"capability": "x", "outcome": "done", "outputs": {}, "failure": {
        "code": "X", "step_id": "t01", "evidence": ["../outside.png"]}})
    if close:
        w.close()
    return w.run_dir


# ---------------------------------------------------------------------------------------------------- timeline


def test_commit_unknown_reads_as_steps_and_one_verdict_handoff():
    tl = load(EVIDENCE / "replay_commit_unknown")
    assert tl.kind == "replay" and (tl.status, tl.outcome) == ("indeterminate", "COMMIT_UNKNOWN")
    steps = [s for s in tl.segments if s.step_id]
    assert [s.step_id for s in steps][-1] == "s13_click" and len(steps) == 13
    last = steps[-1]
    assert last.status == "escalated" and last.effect == "commit" and last.commit_attempted
    assert last.images == ["failure/s13_click.png"]
    [h] = tl.handoffs
    assert (h.reason, h.step_id, h.outcome, h.verdict) == ("COMMIT_UNKNOWN", "s13_click", "verdict", "committed")
    assert h.generations == [1, 2] and h.operator and h.wait_ms and h.request["commit_attempted"] is True


def test_discovery_approvals_attach_to_their_steps_with_the_operator_decision():
    tl = load(EVIDENCE / "discovery_record_payment")
    assert tl.kind == "discovery" and tl.outputs == {"payment_ref": "PAY2610-0028"}
    assert [(h.step_id, h.outcome) for h in tl.handoffs] == [("t08", "operator_approve"), ("t09", "operator_approve")]
    assert all([d["action"] for d in h.decisions] == ["approve"] for h in tl.handoffs)
    t08 = next(s for s in tl.segments if s.step_id == "t08")
    assert t08.screenshot == "screenshots/08-after-07.png" and t08.turn and t08.target["name"] == "Pay"


def test_captcha_handoff_carries_the_captured_human_actions():
    tl = load(EVIDENCE / "replay_captcha_handoff")
    [h] = tl.handoffs
    assert (h.reason, h.outcome) == ("HUMAN_ONLY_LOGIN", "resume") and len(h.human_actions) == 7
    assert [d["action"] for d in h.decisions] == ["resume"]


# -------------------------------------------------------------------------------------------------------- view


def test_view_escapes_bundle_text_and_never_embeds_files_outside_the_bundle(tmp_path):
    (tmp_path / "outside.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 10)
    page = render(load(_bundle(tmp_path, rationale='<script>alert(1)</script>"x"')))
    assert "<script>alert(1)" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "data:image/png" not in page
    assert "http://" not in page.split("<body>")[0].replace("http://app/x", "")  # nothing loaded from the network


def test_cli_view_renders_every_bundle_and_leaves_the_evidence_untouched(tmp_path):
    before = _tree_hashes(EVIDENCE)
    assert main(["view", str(EVIDENCE), "--out", str(tmp_path)]) == 0
    pages = {p.name for p in tmp_path.glob("*.html")}
    assert "index.html" in pages and "replay_commit_unknown.html" in pages and len(pages) >= 7
    assert _tree_hashes(EVIDENCE) == before
    page = (tmp_path / "discovery_record_payment.html").read_text(encoding="utf-8")
    assert page.count('class="card') >= 16 and "data:image/png;base64," in page


# -------------------------------------------------------------------------------------------------------- otel


def _spans(payload):
    return payload["resourceSpans"][0]["scopeSpans"][0]["spans"]


def test_otlp_payload_is_well_formed_deterministic_and_has_no_page_content():
    tl = load(EVIDENCE / "replay_commit_unknown")
    a, b = otel.build(tl), otel.build(load(EVIDENCE / "replay_commit_unknown"))
    assert a == b
    spans = _spans(a)
    ids = {s["spanId"] for s in spans}
    assert len(ids) == len(spans) and all(len(s["spanId"]) == 16 and len(s["traceId"]) == 32 for s in spans)
    assert all(s.get("parentSpanId", next(iter(ids))) in ids for s in spans)
    assert len({s["traceId"] for s in spans}) == 1
    by_name = {s["name"]: s for s in spans}
    assert by_name["s13_click: click button_validate"]["status"]["code"] == otel._ERROR
    assert any(n.startswith("handoff COMMIT_UNKNOWN") for n in by_name) and "waiting for a human" in by_name

    disc = json.dumps(otel.build(load(EVIDENCE / "discovery_record_payment")))
    assert "Navigation menu entries" not in disc and "data:image" not in disc and "screenshots/" not in disc
    assert "Submit the payment form" in disc  # the stated rationale is the point of a trace


def test_export_refuses_an_open_bundle_or_one_the_lint_flags(tmp_path, monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(otel, "send", lambda *a, **k: sent.append(a) or 200)
    open_bundle = _bundle(tmp_path / "a", close=False)
    assert main(["export-otel", str(open_bundle), "--endpoint", "http://127.0.0.1:1"]) == 1
    leaky = _bundle(tmp_path / "b")
    (leaky / "note.txt").write_text("ssn 123-45-6789\n", encoding="utf-8")
    assert main(["export-otel", str(leaky), "--endpoint", "http://127.0.0.1:1"]) == 1
    err = capsys.readouterr().err
    assert "not closed" in err and "lint" in err and "123-45" not in err
    assert sent == []


def test_export_needs_exactly_one_destination(tmp_path):
    assert main(["export-otel", str(EVIDENCE / "replay_policy_denied")]) == 2
    assert main(["export-otel", str(EVIDENCE / "replay_policy_denied"), "--out", str(tmp_path / "x.json"),
                 "--endpoint", "http://localhost:4318"]) == 2
    assert main(["export-otel", str(EVIDENCE / "replay_policy_denied"), "--endpoint", "file:///etc/passwd"]) == 2


def test_endpoint_and_headers():
    assert otel.traces_url("http://h:4318") == "http://h:4318/v1/traces"
    assert otel.traces_url("https://h/api/public/otel/v1/traces") == "https://h/api/public/otel/v1/traces"
    assert otel.parse_headers(["X-B=2"], "Authorization=Basic%20abc,X-A=1") == {
        "Authorization": "Basic abc", "X-A": "1", "X-B": "2"}


def test_export_posts_otlp_json_to_a_collector(tmp_path, monkeypatch, capsys):
    got: list[tuple[str, dict, bytes]] = []

    class Collector(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            got.append((self.path, dict(self.headers), self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Collector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Bearer%20tok")
    try:
        code = main(["export-otel", str(EVIDENCE / "replay_captcha_handoff"),
                     "--endpoint", f"http://127.0.0.1:{server.server_port}"])
    finally:
        server.shutdown()
    assert code == 0
    [(path, headers, body)] = got
    assert path == "/v1/traces" and headers["Content-Type"] == "application/json"
    assert headers["Authorization"] == "Bearer tok" and "tok" not in capsys.readouterr().out
    spans = _spans(json.loads(body))
    assert spans[0]["name"] == "cua.replay dolibarr.lookup_customer_outstanding"
    assert int(spans[0]["startTimeUnixNano"]) == int(load(EVIDENCE / "replay_captcha_handoff").start.timestamp() * 1e6) * 1000


def test_out_writes_one_payload_for_several_bundles(tmp_path):
    assert main(["export-otel", str(EVIDENCE), "--out", str(tmp_path / "t.json")]) == 0
    data = json.loads((tmp_path / "t.json").read_text(encoding="utf-8"))
    assert len(data["resourceSpans"]) == len(list(EVIDENCE.glob("*/events.jsonl")))


@pytest.mark.parametrize("bundle", sorted(p.parent.name for p in EVIDENCE.glob("*/events.jsonl")))
def test_every_committed_bundle_loads_renders_and_exports(bundle):
    tl = load(EVIDENCE / bundle)
    assert tl.start <= tl.end and tl.segments and tl.start.tzinfo is not None
    assert render(tl).startswith("<!doctype html>")
    assert _spans(otel.build(tl))[0]["traceId"]
    assert datetime.now(UTC) > tl.start


# ----------------------------------------------------------------------------------------------------- lineage


def test_payment_discovery_lines_up_turn_by_turn_with_its_draft():
    tl = load(EVIDENCE / "discovery_record_payment")
    lin = tl.lineage
    assert lin is not None and lin.unmatched == []
    assert lin.counts() == {"turns": 14, "steps": 17, "compiled": 11, "checks": 6, "dropped": 2, "unmatched": 0}
    assert lin.step_ids(8) == ["s09_click"] and lin.effect_changes == {"s09_click": ("commit", "read")}
    assert lin.step_ids(9) == ["s10_assert", "s11_assert", "s12_assert", "s13_click", "s14_assert"]
    assert [x.role for x in lin.by_turn[9]] == ["check_before"] * 3 + ["compiled", "check_after"]
    assert sorted(lin.dropped) == [11, 13, 14] and lin.dropped[14].startswith("done")
    covered = [x.step["id"] for links in lin.by_turn.values() for x in links]
    assert sorted(covered) == sorted(s["id"] for s in lin.steps)  # every step has exactly one source turn


def test_lookup_discovery_drops_the_attempt_the_recorder_refused():
    lin = load(EVIDENCE / "discovery_lookup_customer_outstanding").lineage
    assert lin.step_ids(2) == ["s02_click", "s03_assert"] and lin.step_ids(4) == ["s04_extract"]
    assert 3 in lin.dropped and "did not succeed" in lin.dropped[3] and lin.unmatched == []


def test_lineage_mirrors_the_compiler_rules():
    """evidence may not import the compiler; this pins the copy to it, on today's compiler output."""
    from cua.compiler import compile as compiler
    from cua.evidence import lineage

    assert lineage.KEPT == compiler._KEPT
    for bundle, name in (("discovery_record_payment", "record_invoice_payment"),
                         ("discovery_lookup_customer_outstanding", "lookup_customer_outstanding")):
        trace = json.loads((EVIDENCE / bundle / "trace.json").read_text(encoding="utf-8"))
        for a in trace["actions"]:
            r = a["action"].get("rationale") or ""
            assert lineage.intent_of(r) == compiler._intent(r)
        fresh = json.loads(compiler.compile_file(EVIDENCE / bundle / "trace.json", ROOT / "apps" / "dolibarr" / "profile.json",
                                                 name=name, discovered_by="x"))
        lin = lineage.build(trace, fresh)
        assert lin.unmatched == [] and sum(len(v) for v in lin.by_turn.values()) == len(fresh["steps"])


def test_a_hand_edited_step_is_reported_not_guessed():
    from cua.evidence import lineage

    trace = json.loads((EVIDENCE / "discovery_record_payment" / "trace.json").read_text(encoding="utf-8"))
    draft = json.loads((EVIDENCE / "discovery_record_payment" / "capability.draft.json").read_text(encoding="utf-8"))
    draft["steps"][2]["intent"] = "Something a reviewer wrote"
    lin = lineage.build(trace, draft)
    assert [s["id"] for s in lin.unmatched] == ["s03_click"] and lin.step_ids(4) == ["s04_select"]


def test_view_and_export_show_the_compiled_steps():
    tl = load(EVIDENCE / "discovery_record_payment")
    page = render(tl)
    assert "Discovery → capability" in page and "14 model turns became 17 replay steps" in page
    assert "Compiled to s10_assert, s11_assert, s12_assert, s13_click, s14_assert" in page
    assert "compiled as read because the click only opens the confirmation prompt" in page
    spans = {s["name"]: s for s in _spans(otel.build(tl))}
    attrs = {a["key"]: a["value"] for a in spans['t08: click button "Pay"']["attributes"]}
    assert attrs["cua.compiled_to"] == {"arrayValue": {"values": [{"stringValue": "s09_click"}]}}
    assert "Discovery → capability" not in render(load(EVIDENCE / "replay_commit_unknown"))
