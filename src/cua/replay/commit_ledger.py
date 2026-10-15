"""Durable request_id ledger: a commit runs at most once per request_id, across runs and restarts.

States: PREPARED (row created, nothing clicked) -> ATTEMPTED (the marker, written BEFORE the commit click by a
conditional UPDATE ... WHERE state='PREPARED', so two runs cannot both win) -> DONE (confirmed or declared rejection,
with the stored `RunResult`) or UNKNOWN (the outcome is not known). Opening the ledger sweeps ATTEMPTED -> UNKNOWN:
a process that died mid-commit cannot know. The same request_id with other inputs is rejected; the same request_id and
inputs after DONE or UNKNOWN returns the stored result and the caller does no UI action at all.

The inputs hash is over the declared inputs except `request_id`; it is stored, the values are not.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from cua.replay.result import RunResult

_SCHEMA = "CREATE TABLE IF NOT EXISTS commits (request_id TEXT PRIMARY KEY, inputs_hash TEXT NOT NULL, " \
          "state TEXT NOT NULL, result TEXT)"


class RequestIdReused(Exception):
    """The request_id was used before with different inputs."""


def inputs_hash(inputs: dict[str, str]) -> str:
    body = {k: v for k, v in inputs.items() if k != "request_id"}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


class CommitLedger:
    def __init__(self, path: Path | str = ":memory:") -> None:
        self.db = sqlite3.connect(str(path), isolation_level=None)  # autocommit: every write is durable at once
        self.db.execute(_SCHEMA)
        self.db.execute("UPDATE commits SET state='UNKNOWN' WHERE state='ATTEMPTED'")

    def close(self) -> None:
        self.db.close()

    def state(self, request_id: str) -> str | None:
        row = self.db.execute("SELECT state FROM commits WHERE request_id=?", (request_id,)).fetchone()
        return row[0] if row else None

    def begin(self, request_id: str, digest: str) -> tuple[str, RunResult | None]:
        """Create the PREPARED row or read the existing one: (state, stored result)."""
        self.db.execute("INSERT OR IGNORE INTO commits (request_id, inputs_hash, state) VALUES (?, ?, 'PREPARED')",
                        (request_id, digest))
        stored, state, result = self.db.execute(
            "SELECT inputs_hash, state, result FROM commits WHERE request_id=?", (request_id,)).fetchone()
        if stored != digest:
            raise RequestIdReused(request_id)
        return state, RunResult.model_validate_json(result) if result else None

    def attempt(self, request_id: str) -> bool:
        """PREPARED -> ATTEMPTED; False when another run already took it (or it is past PREPARED)."""
        return self.db.execute("UPDATE commits SET state='ATTEMPTED' WHERE request_id=? AND state='PREPARED'",
                               (request_id,)).rowcount == 1

    def withdraw(self, request_id: str) -> None:
        """ATTEMPTED -> PREPARED, only for a click that provably never happened (policy denial, no such element)."""
        self.db.execute("UPDATE commits SET state='PREPARED' WHERE request_id=? AND state='ATTEMPTED'", (request_id,))

    def finish(self, request_id: str, result: RunResult) -> None:
        """DONE with the result for success or a declared outcome; UNKNOWN for an attempt that ended otherwise.
        A run that never attempted leaves the row PREPARED so the request can be retried."""
        if result.status in ("success", "business_outcome"):
            self.db.execute("UPDATE commits SET state='DONE', result=? WHERE request_id=? AND state='ATTEMPTED'",
                            (result.model_dump_json(), request_id))
        else:
            self.db.execute("UPDATE commits SET state='UNKNOWN', result=? WHERE request_id=? AND state='ATTEMPTED'",
                            (result.model_dump_json(), request_id))
