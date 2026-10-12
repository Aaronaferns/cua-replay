"""The run bundle writer: the single persistence path for everything a run produces.

Owner: Agent C. Implements the `EvidenceWriter` protocol from `cua.evidence.events`.

Layout (`runs/<run_id>/` is git-ignored; curated bundles are promoted into `evidence/` by hand):

    manifest.json   run id, timestamps, status and, once closed, every file with its size and sha256
    events.jsonl    one `EvidenceEvent` per line; `seq` is stamped here, starts at 0 and only increases
    ...             whatever the run writes through `json`, `text` and `image`

Guarantees:
- Every text and JSON payload and every event is redacted before it touches disk.
- Relative paths are confined to the run directory (no absolute paths, no `..`, no reserved names compared
  case-insensitively, no Windows device names or trailing dots/spaces, no `.tmp` names) and are never substituted:
  a path that contains a known secret or that a redaction pattern would change is refused.
- Writes are atomic (temp file + replace). Appending an event is flushed before `event()` returns.
- `image()` writes the PNG bytes it is given. Pixels cannot be redacted here: masking is the surface's job (declared
  masks plus a generic input/select/textarea/td mask) and happens before the bytes reach this writer. The writer only
  checks that the bytes are really a PNG, so the image path cannot be used to smuggle unredacted text.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic_core import to_jsonable_python

from cua.evidence.events import EventActor, EvidenceEvent
from cua.safety.redact import PATTERN_NAMES, Redactor

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
EVENTS_FILE = "events.jsonl"
MANIFEST_FILE = "manifest.json"
_RESERVED = {EVENTS_FILE, MANIFEST_FILE}
_RESERVED_FOLDED = {name.casefold() for name in _RESERVED}
# Windows device names are special in every directory and with any extension ("nul.txt" opens the NUL device)
_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$"}
    | {f"{d}{i}" for d in ("com", "lpt") for i in "123456789¹²³"}
)
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


class EvidenceError(Exception):
    """A write was refused (bad path, not a PNG, closed bundle). Messages never contain payload content."""


def default_runs_root() -> Path:
    from cua.config import REPO_ROOT

    return REPO_ROOT / "runs"


class RunBundleWriter:
    """Writes one run bundle. Thread-safe; usable as a context manager (closing finalizes the manifest).

    `redactor` defaults to one built from the environment's secrets. Register run-supplied values (capability inputs,
    typed text) with `add_secret_values` as soon as they are known; every later write redacts them.
    Re-opening an existing run id appends: `seq` continues after the last event on disk.
    """

    def __init__(self, run_id: str, *, root: Path | None = None, redactor: Redactor | None = None):
        if not _RUN_ID.fullmatch(run_id):
            raise EvidenceError("run_id must be 1-100 characters of letters, digits, '.', '_' or '-'")
        self.run_id = run_id
        self.redactor = redactor if redactor is not None else Redactor.from_settings()
        self.run_dir = ((root if root is not None else default_runs_root()) / run_id).resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._created = datetime.now(UTC)
        self._seq = self._next_seq_on_disk()
        self._write_manifest(status="open")

    # -------------------------------------------------------------------------------------------- public API

    def add_secret_values(self, values: list[str], *, label: str = "input") -> None:
        """Make values known to the redactor from now on (input values, typed text, per-run credentials)."""
        self.redactor.add_many(values, label=label)

    def event(self, event: EvidenceEvent) -> None:
        """Append an event. `seq` is stamped by the writer (any value passed in is replaced)."""
        with self._lock:
            self._ensure_open()
            stamped = event.model_copy(update={"seq": self._seq, "run_id": self.run_id})
            payload = self.redactor.data(stamped.model_dump(mode="json"))
            line = self.redactor.text(json.dumps(payload, ensure_ascii=False), known_only=True)
            with open(self.run_dir / EVENTS_FILE, "a", encoding="utf-8", newline="\n") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
            self._seq += 1

    def emit(self, kind: str, *, actor: EventActor = "system", **fields: Any) -> EvidenceEvent:
        """Build an `EvidenceEvent` (timestamp, run id and seq filled in) and append it."""
        event = EvidenceEvent(ts=datetime.now(UTC), seq=0, run_id=self.run_id, actor=actor, kind=kind, **fields)
        self.event(event)
        return event

    def json(self, relpath: str, obj: Any) -> Path:
        with self._lock:
            self._ensure_open()
            path = self._path(relpath)
            payload = self.redactor.data(to_jsonable_python(obj, fallback=str))
            text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            return self._atomic_write(path, self.redactor.text(text, known_only=True).encode("utf-8"))

    def text(self, relpath: str, text: str) -> Path:
        with self._lock:
            self._ensure_open()
            path = self._path(relpath)
            return self._atomic_write(path, self.redactor.text(text).encode("utf-8"))

    def image(self, relpath: str, png: bytes) -> Path:
        with self._lock:
            self._ensure_open()
            path = self._path(relpath)
            if not png.startswith(PNG_MAGIC):
                raise EvidenceError("image bytes are not a PNG")
            return self._atomic_write(path, png)

    def close(self, *, status: str = "closed") -> Path:
        """Finalize the manifest (file list with sizes and sha256). Further writes are refused."""
        with self._lock:
            if not self._closed:
                self._write_manifest(status=status)
                self._closed = True
            return self.run_dir / MANIFEST_FILE

    def __enter__(self) -> RunBundleWriter:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close(status="closed" if exc_type is None else "aborted")

    # ------------------------------------------------------------------------------------------- internals

    def _ensure_open(self) -> None:
        if self._closed:
            raise EvidenceError("bundle is closed")

    def _next_seq_on_disk(self) -> int:
        events = self.run_dir / EVENTS_FILE
        if not events.exists():
            return 0
        with open(events, encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())

    def _path(self, relpath: str) -> Path:
        pure = PurePosixPath(relpath.replace("\\", "/"))
        if (
            not relpath
            or pure.is_absolute()
            or ":" in relpath  # drive letters and NTFS alternate data streams
            or any(part in ("", ".", "..") for part in pure.parts)
            or "\x00" in relpath
        ):
            raise EvidenceError("relpath must be a plain relative path inside the run directory")
        for part in pure.parts:
            folded = part.casefold()
            if (
                part.endswith((".", " "))  # Windows strips them, so "manifest.json." names the manifest
                or folded.endswith(".tmp")  # the atomic-write scratch name
                or folded.split(".")[0].rstrip(" ") in _DEVICE_NAMES
                or any(ord(c) < 0x20 for c in part)
            ):
                raise EvidenceError("relpath has a name that is not portable or is used by the writer")
        if relpath != self.redactor.text(relpath) or self.redactor.contains_known(relpath):
            # refuse rather than substitute: a renamed file would break every reference to it
            raise EvidenceError("relpath contains a secret or sensitive value")
        path = (self.run_dir / pure).resolve()
        if not path.is_relative_to(self.run_dir):
            raise EvidenceError("relpath escapes the run directory")
        if path.relative_to(self.run_dir).as_posix().casefold() in _RESERVED_FOLDED:
            raise EvidenceError("relpath is reserved for the bundle")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _atomic_write(self, path: Path, data: bytes) -> Path:
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return path

    def _write_manifest(self, *, status: str) -> None:
        files = []
        if status != "open":
            for path in sorted(p for p in self.run_dir.rglob("*") if p.is_file()):
                rel = path.relative_to(self.run_dir).as_posix()
                if rel == MANIFEST_FILE or rel.endswith(".tmp"):
                    continue
                data = path.read_bytes()
                files.append({"path": rel, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        manifest = {
            "run_id": self.run_id,
            "status": status,
            "created": self._created.isoformat(),
            "updated": datetime.now(UTC).isoformat(),
            "events": self._seq,
            "redaction": {"known_values": self.redactor.known_value_count, "patterns": list(PATTERN_NAMES)},
            "files": files,
        }
        self._atomic_write(self.run_dir / MANIFEST_FILE, (json.dumps(manifest, indent=2) + "\n").encode("utf-8"))
