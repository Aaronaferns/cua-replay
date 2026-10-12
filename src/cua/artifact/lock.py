"""Canonical JSON and the lock hash that approval binds to.

    lock_hash = sha256("cua.lock/v1" NUL canonical(capability) NUL canonical(profile))

Everything that changes what replay does is inside the lock: the capability body *and* the profile (states,
meanings, login form, commit routes). Editing either one changes the hash, which invalidates an approval.

Hashes are computed on the *validated model*, never on file bytes: line endings, indentation, key order and
omitted `null`s cannot change them. Canonical form = model dump (JSON mode, aliases, `None` fields dropped) as
compact JSON with sorted keys and UTF-8 text. JSON never contains a raw NUL, so the separators are unambiguous.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from cua.artifact.schema import Capability, Profile

M = TypeVar("M", bound=BaseModel)


def plain(model: BaseModel) -> Any:
    """The model as JSON-compatible data, in the one normal form used for hashing and for writing files."""
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


def canonical(data: Any) -> bytes:
    if isinstance(data, BaseModel):
        data = plain(data)
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_hash(model: BaseModel) -> str:
    """Hash of one document in normal form (e.g. the draft hash reproduced from a trace)."""
    return sha256_hex(canonical(model))


def lock_hash(capability: Capability, profile: Profile) -> str:
    parts = [b"cua.lock/v1", canonical(capability), canonical(profile)]
    return sha256_hex(b"\x00".join(parts))


def dumps(model: BaseModel) -> str:
    """Deterministic, human-readable file text: field order of the schema, 2-space indent, LF, trailing newline."""
    return json.dumps(plain(model), indent=2, ensure_ascii=False) + "\n"


def load(model_type: type[M], path: Path) -> M:
    return model_type.model_validate_json(path.read_bytes())


def load_capability(path: Path) -> Capability:
    return load(Capability, path)


def load_profile(path: Path) -> Profile:
    return load(Profile, path)
