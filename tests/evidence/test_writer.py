"""The run bundle writer: one persistence path, redaction before disk, confined paths, monotonic events."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cua.control.lease import AUTOMATION, LeaseToken
from cua.evidence.events import EvidenceEvent
from cua.evidence.writer import EvidenceError, RunBundleWriter
from cua.safety.policy import PolicyDecision
from cua.safety.redact import Redactor

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
SECRET = "S3cret/Value&1"


@pytest.fixture
def writer(tmp_path: Path) -> RunBundleWriter:
    return RunBundleWriter("run-1", root=tmp_path, redactor=Redactor([SECRET]))


def event(**overrides) -> EvidenceEvent:
    data = {"ts": datetime.now(UTC), "seq": 999, "run_id": "ignored", "actor": "replay", "kind": "action"}
    data.update(overrides)
    return EvidenceEvent(**data)


def read_events(w: RunBundleWriter) -> list[dict]:
    return [json.loads(line) for line in (w.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]


def all_bytes(w: RunBundleWriter) -> bytes:
    return b"".join(p.read_bytes() for p in w.run_dir.rglob("*") if p.is_file())


def test_bundle_directory_is_runs_run_id(tmp_path):
    w = RunBundleWriter("2026-10-12_lookup", root=tmp_path, redactor=Redactor())
    assert w.run_dir == (tmp_path / "2026-10-12_lookup").resolve()
    assert json.loads((w.run_dir / "manifest.json").read_text())["status"] == "open"


def test_default_root_is_the_git_ignored_runs_directory():
    from cua.config import REPO_ROOT
    from cua.evidence.writer import default_runs_root

    assert default_runs_root() == REPO_ROOT / "runs"
    assert "runs/" in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_events_have_monotonic_seq_stamped_by_the_writer(writer):
    for i in range(5):
        writer.event(event(seq=100 - i, kind=f"k{i}"))
    events = read_events(writer)
    assert [e["seq"] for e in events] == [0, 1, 2, 3, 4]
    assert [e["kind"] for e in events] == [f"k{i}" for i in range(5)]
    assert {e["run_id"] for e in events} == {"run-1"}  # run id comes from the bundle, not the caller


def test_seq_is_monotonic_across_threads(writer):
    def worker():
        for _ in range(25):
            writer.event(event())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [e["seq"] for e in read_events(writer)] == list(range(100))


def test_reopening_a_run_continues_the_sequence(tmp_path):
    first = RunBundleWriter("r", root=tmp_path, redactor=Redactor())
    first.event(event())
    first.event(event())
    second = RunBundleWriter("r", root=tmp_path, redactor=Redactor())
    second.event(event())
    assert [e["seq"] for e in read_events(second)] == [0, 1, 2]


def test_event_payloads_are_redacted_before_disk(writer):
    writer.event(
        event(
            action={"kind": "type", "text": "{{customer_code}}", "echo": SECRET},
            rationale=f"typed {SECRET} into the search box",
            detail={"headers": {"Cookie": "DOLSESSID_x=abc123def456"}, "url": f"/x?token=abcd1234efgh&q={SECRET}"},
            policy=PolicyDecision(allowed=False, rule="origin_allowlist", reason="origin is not on the allowlist"),
            lease=LeaseToken(holder=AUTOMATION, generation=2),
        )
    )
    raw = (writer.run_dir / "events.jsonl").read_text(encoding="utf-8")
    for leak in (SECRET, "abc123def456", "abcd1234efgh"):
        assert leak not in raw
    stored = read_events(writer)[0]
    assert stored["action"]["text"] == "{{customer_code}}"
    assert stored["lease"] == {"holder": "automation", "generation": 2}
    assert stored["policy"]["rule"] == "origin_allowlist"


def test_json_text_and_image_are_redacted_or_validated(writer):
    j = writer.json("trace/step.json", {"typed": SECRET, "n": 1, "password": "x", "when": datetime(2026, 1, 1, tzinfo=UTC)})
    t = writer.text("transcript.txt", f"user typed {SECRET}\nCookie: a=b\n")
    i = writer.image("shots/a.png", PNG)
    assert j == writer.run_dir / "trace" / "step.json" and t.name == "transcript.txt" and i.read_bytes() == PNG
    assert SECRET.encode() not in all_bytes(writer)
    assert json.loads(j.read_text(encoding="utf-8"))["password"] == "[REDACTED]"
    assert "Cookie: [REDACTED]" in t.read_text(encoding="utf-8")


def test_json_with_the_secret_in_escaped_form_is_redacted(writer):
    writer.json("a.json", {"u": f"https://x/?v={SECRET.replace('/', '%2F').replace('&', '%26')}"})
    assert b"S3cret" not in all_bytes(writer)


def test_json_accepts_pydantic_models(writer):
    path = writer.json("decision.json", PolicyDecision(allowed=False, rule="r", reason="why"))
    assert json.loads(path.read_text(encoding="utf-8"))["rule"] == "r"


def test_secret_added_mid_run_is_redacted_from_then_on(writer):
    writer.text("a.txt", "value zz-run-input-77 visible")
    writer.add_secret_values(["zz-run-input-77"])
    writer.text("b.txt", "value zz-run-input-77 hidden")
    assert "zz-run-input-77" in (writer.run_dir / "a.txt").read_text(encoding="utf-8")
    assert "zz-run-input-77" not in (writer.run_dir / "b.txt").read_text(encoding="utf-8")


def test_image_must_be_a_png(writer):
    with pytest.raises(EvidenceError):
        writer.image("shot.png", f"not an image {SECRET}".encode())
    assert not (writer.run_dir / "shot.png").exists()


@pytest.mark.parametrize(
    "relpath",
    ["../escape.txt", "a/../../escape.txt", "/abs.txt", "C:/abs.txt", "C:\\abs.txt", "..\\x.txt", "", "a.txt:stream",
     "events.jsonl", "manifest.json", "./manifest.json"],
)
def test_paths_are_confined_to_the_run_directory(writer, tmp_path, relpath):
    with pytest.raises(EvidenceError):
        writer.text(relpath, "x")
    assert not (tmp_path / "escape.txt").exists()


@pytest.mark.parametrize("run_id", ["", "../x", "a/b", "a\\b", ".hidden", "x" * 101])
def test_run_ids_are_plain_names(tmp_path, run_id):
    with pytest.raises(EvidenceError):
        RunBundleWriter(run_id, root=tmp_path, redactor=Redactor())


def test_manifest_lists_files_with_hashes_when_closed(writer):
    writer.event(event())
    writer.text("t.txt", "hello")
    writer.image("s/a.png", PNG)
    manifest_path = writer.close()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "closed" and manifest["events"] == 1
    by_path = {f["path"]: f for f in manifest["files"]}
    assert set(by_path) == {"events.jsonl", "t.txt", "s/a.png"}
    assert by_path["s/a.png"]["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert manifest["redaction"]["known_values"] == 1 and SECRET not in manifest_path.read_text(encoding="utf-8")
    with pytest.raises(EvidenceError):
        writer.text("late.txt", "x")


def test_context_manager_marks_aborted_runs(tmp_path):
    with pytest.raises(RuntimeError), RunBundleWriter("r2", root=tmp_path, redactor=Redactor()) as w:
        raise RuntimeError("boom")
    assert json.loads((w.run_dir / "manifest.json").read_text())["status"] == "aborted"


def test_no_temp_files_are_left_behind(writer):
    writer.text("a.txt", "x")
    writer.json("b.json", {})
    assert not list(writer.run_dir.rglob("*.tmp"))


def test_emit_builds_a_full_event(writer):
    e = writer.emit("lease", actor="system", detail={"generation": 3})
    stored = read_events(writer)[0]
    assert stored["kind"] == "lease" and stored["detail"] == {"generation": 3} and stored["seq"] == 0
    assert e.run_id == "run-1"
