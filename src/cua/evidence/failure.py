"""Failure evidence: what a failed step leaves behind in the run bundle.

Owner: Agent C. Called by the replay interpreter (and by anything else that wants the same artifacts) when a step
fails or a run ends indeterminate:

    evidence = await capture_failure_evidence(surface, writer, masks=[...], step_id="s3")
    failure.evidence = evidence.paths          # bundle-relative paths, ready for `Failure.evidence`

Two files, both written through the evidence writer:

- `failure/<step_id>.png`   the page with the declared `masks` and the generic `input, select, textarea, td` mask
  painted over it (`surface.masked_screenshot(masks, generic=True)`); pixels cannot be redacted afterwards, so the
  surface masks before the bytes exist;
- `failure/<step_id>.snapshot.json`  the observation (facts, scoped snapshot, dialog snapshot, menu summary) with
  no screenshot; the writer redacts known values and sensitive patterns.

Capturing is best effort and never raises: a failure while explaining a failure must not replace it. What could not
be captured is listed in `errors` as value-free text (see `cua.safety.redact.safe_error_message`). Imports no surface
module: `surface` is duck-typed against the two methods used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from cua.evidence.events import EvidenceWriter
from cua.safety.redact import safe_error_message

_STEP_ID = re.compile(r"[^A-Za-z0-9_-]+")


class _Surface(Protocol):
    async def observe(self, *, screenshot: bool = True, settle: bool = False) -> Any: ...
    async def masked_screenshot(self, masks: list[Any], *, generic: bool) -> bytes: ...


@dataclass
class FailureEvidence:
    screenshot: str | None = None  # bundle-relative path
    snapshot: str | None = None  # bundle-relative path
    errors: list[str] = field(default_factory=list)  # what could not be captured, without values

    @property
    def paths(self) -> list[str]:
        return [p for p in (self.screenshot, self.snapshot) if p is not None]


def _stem(step_id: str | None) -> str:
    cleaned = _STEP_ID.sub("_", step_id or "run").strip("_")
    return f"failure/{cleaned[:60] or 'run'}"


async def capture_failure_evidence(
    surface: _Surface, writer: EvidenceWriter, *, masks: list[Any], step_id: str | None
) -> FailureEvidence:
    """Write the masked screenshot and the redacted snapshot for a failed step; return where they went.

    `masks` are the surface elements the capability declares sensitive (resolved by the caller, possibly empty); the
    generic input/select/textarea/td mask is always added. `step_id` names the files (`None` for a failure before
    the first step).
    """
    out = FailureEvidence()
    stem = _stem(step_id)
    try:
        png = await surface.masked_screenshot(list(masks), generic=True)
        out.screenshot = writer.image(f"{stem}.png", png).relative_to(writer.run_dir).as_posix()
    except Exception as e:  # noqa: BLE001 - best effort, never replaces the failure being explained
        out.errors.append(f"screenshot: {safe_error_message(e)}")
    try:
        observation = await surface.observe(screenshot=False)
        body = observation.model_dump(mode="json", exclude={"screenshot_png"})
        path = writer.json(f"{stem}.snapshot.json", {"step_id": step_id, **body})
        out.snapshot = path.relative_to(writer.run_dir).as_posix()
    except Exception as e:  # noqa: BLE001
        out.errors.append(f"snapshot: {safe_error_message(e)}")
    return out
