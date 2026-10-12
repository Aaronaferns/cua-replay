"""Failure evidence: masked screenshot + redacted snapshot through the writer, best effort, value-free errors."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cua.evidence.failure import capture_failure_evidence
from cua.evidence.writer import RunBundleWriter
from cua.safety.redact import Redactor
from cua.surface.base import Observation, PageFacts

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
SECRET = "CU2601-00901"


class FakeSurface:
    def __init__(self, *, snapshot: str = "heading 'Customer'", fail_screenshot=None, fail_observe=None):
        self.snapshot = snapshot
        self.fail_screenshot = fail_screenshot
        self.fail_observe = fail_observe
        self.screenshot_calls: list[tuple[list, bool]] = []
        self.observe_calls: list[dict] = []

    async def masked_screenshot(self, masks, *, generic):
        self.screenshot_calls.append((masks, generic))
        if self.fail_screenshot:
            raise self.fail_screenshot
        return PNG

    async def observe(self, *, screenshot=True, settle=False):
        self.observe_calls.append({"screenshot": screenshot, "settle": settle})
        if self.fail_observe:
            raise self.fail_observe
        return Observation(
            epoch=3,
            facts=PageFacts(url="http://localhost:8087/comm/card.php", title="Customer"),
            snapshot=self.snapshot,
            menu_summary=["Home"],
            screenshot_png=PNG if screenshot else None,
        )


@pytest.fixture
def writer(tmp_path: Path) -> RunBundleWriter:
    return RunBundleWriter("run-f", root=tmp_path, redactor=Redactor([SECRET]))


async def test_writes_masked_screenshot_and_redacted_snapshot(writer):
    surface = FakeSurface(snapshot=f"cell '{SECRET}' [ref=e4]")
    mask = object()
    evidence = await capture_failure_evidence(surface, writer, masks=[mask], step_id="s3")

    assert surface.screenshot_calls == [([mask], True)]  # declared masks + the generic mask
    assert surface.observe_calls == [{"screenshot": False, "settle": False}]
    assert evidence.errors == []
    assert evidence.paths == ["failure/s3.png", "failure/s3.snapshot.json"]
    assert (writer.run_dir / "failure/s3.png").read_bytes() == PNG
    snapshot = (writer.run_dir / "failure/s3.snapshot.json").read_text(encoding="utf-8")
    assert SECRET not in snapshot and "REDACTED" in snapshot
    body = json.loads(snapshot)
    assert body["step_id"] == "s3" and body["facts"]["title"] == "Customer"
    assert "screenshot_png" not in body


async def test_step_id_is_sanitized_and_may_be_absent(writer):
    surface = FakeSurface()
    a = await capture_failure_evidence(surface, writer, masks=[], step_id="../../x y")
    b = await capture_failure_evidence(surface, writer, masks=[], step_id=None)
    assert a.screenshot == "failure/x_y.png"
    assert b.screenshot == "failure/run.png"


async def test_a_failing_screenshot_does_not_stop_the_snapshot_or_raise(writer):
    surface = FakeSurface(fail_screenshot=RuntimeError(f"boom {SECRET}"))
    evidence = await capture_failure_evidence(surface, writer, masks=[], step_id="s1")
    assert evidence.screenshot is None and evidence.snapshot == "failure/s1.snapshot.json"
    assert evidence.errors == ["screenshot: RuntimeError"]  # the type only: a third-party message may quote values
    assert SECRET not in " ".join(evidence.errors)


async def test_a_failing_observation_does_not_raise(writer):
    surface = FakeSurface(fail_observe=RuntimeError("x"))
    evidence = await capture_failure_evidence(surface, writer, masks=[], step_id="s1")
    assert evidence.paths == ["failure/s1.png"]
    assert evidence.errors == ["snapshot: RuntimeError"]


async def test_a_non_png_from_the_surface_is_reported_not_raised(writer):
    surface = FakeSurface()

    async def bad(masks, *, generic):
        return b"not a png"

    surface.masked_screenshot = bad
    evidence = await capture_failure_evidence(surface, writer, masks=[], step_id="s2")
    assert evidence.screenshot is None
    assert evidence.errors and evidence.errors[0].startswith("screenshot: EvidenceError")
