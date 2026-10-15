"""Console approvals for discovery: a commit-effect action waits for an operator's single-use approval."""

from __future__ import annotations

import itertools
from collections.abc import Awaitable, Callable

from cua.control.console import InterventionHub
from cua.control.interventions import InterventionRequest

APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
Approver = Callable[[str, int], Awaitable[str | None]]


def console_approver(
    hub: InterventionHub, *, capability: str = "discovery.commit", url: str = "http://localhost/", step_id: str | None = None
) -> Approver:
    """`approver(action_hash, generation)` escalates through `hub` and returns the approval id the operator issued, or
    None (abort, SLA, verdict, or the lease moved on since `generation`). The id is bound to `action_hash` and to the
    generation the automation holds after the release; redeem it once with `hub.approvals.consume`."""
    counter = itertools.count(1)

    async def approver(action_hash: str, generation: int) -> str | None:
        assert hub.lease is not None and hub.writer is not None
        if hub.lease.generation != generation:
            return None
        request = InterventionRequest.create(
            id=f"approval-{action_hash[:12]}-{next(counter)}", run_id=hub.writer.run_dir.name, capability=capability,
            reason=APPROVAL_REQUIRED, url=url, lease=hub.lease.token(), step_id=step_id, effect_so_far="read",
        )
        outcome = await hub.escalate(request, action_hash=action_hash)
        if outcome.kind != "approved":
            return None
        check = hub.approvals.consume(outcome.approval_id, action_hash=action_hash, generation=hub.lease.generation)
        return outcome.approval_id if check.ok else None

    return approver
