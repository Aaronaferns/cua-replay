"""The M2 shape in miniature: trace -> compiled draft -> model-free replay for a different input."""

from __future__ import annotations

import json

from fakes import BASE, AllowGuard, Clock, FakeDolibarr, lease
from sample_docs import dolibarr_profile
from trace_builder import build_trace

from cua.artifact.lock import dumps
from cua.artifact.schema import Capability
from cua.compiler.compile import compile_to_json
from cua.replay.interpreter import replay

PROFILE = dolibarr_profile()


async def run_compiled(code: str):
    text = compile_to_json(build_trace(), PROFILE, name="lookup_customer_outstanding", discovered_by="alice")
    cap = Capability.model_validate(json.loads(text))
    assert dumps(cap) == text  # the file form is the normal form
    guard = AllowGuard()
    clock = Clock()
    return await replay(cap, PROFILE, {"customer_code": code}, surface=FakeDolibarr(guard), guard=guard,
                        lease=lease, base_url=BASE, now_ms=clock.now, sleep=clock.sleep), guard


async def test_compiled_draft_replays_for_another_customer_without_drift():
    result, guard = await run_compiled("CU1910-00021")
    assert result.status == "success", result.failure
    assert result.outputs == {"outstanding_amount": "1234.50"}
    assert result.drift == []
    assert [q.kind for q in guard.requests] == ["navigate", "navigate", "type", "press", "click", "extract"]


async def test_compiled_draft_catches_the_wrong_customer():
    text = compile_to_json(build_trace(), PROFILE, name="lookup_customer_outstanding", discovered_by="alice")
    cap = Capability.model_validate(json.loads(text))
    guard = AllowGuard()
    fake = FakeDolibarr(guard)
    fake.card_code_override = "CU1212-0005"  # the card shows someone else
    clock = Clock()
    result = await replay(cap, PROFILE, {"customer_code": "CU1910-00021"}, surface=fake, guard=guard, lease=lease,
                          base_url=BASE, now_ms=clock.now, sleep=clock.sleep)
    assert result.status == "failure" and result.failure.code == "IDENTITY_MISMATCH"
