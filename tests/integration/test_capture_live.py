"""Human-action capture on the real Dolibarr page, through the real guarded session (Dolibarr forms all carry a hidden
`action` field, which is what shadowed `form.action` in the S2 spike's script). Needs the target; skipped otherwise."""

from __future__ import annotations

import json

import pytest

from cua.control.capture import RequestLog, install_capture
from cua.control.runtime import open_session

pytestmark = pytest.mark.live

TYPED = "CU2601-00901"


async def test_capture_on_dolibarr_records_the_search_without_the_typed_value(tmp_path):
    async with open_session(run_id="capture-live", evidence_root=tmp_path) as session:
        page = session.surface.page
        context = page.context
        events = []
        log = RequestLog(session.profile)
        await install_capture(context, on_event=events.append)
        log.attach(context)

        await page.goto(f"{session.base_url}/societe/list.php?type=c")
        await page.locator("input[name=search_customer_code]").click()
        await page.keyboard.type(TYPED)
        await page.keyboard.press("Enter")
        await page.wait_for_load_state()
        await page.wait_for_timeout(300)

    kinds = [e.kind for e in events]
    assert "click" in kinds and "enter" in kinds and "submit" in kinds, kinds
    submit = next(e for e in events if e.kind == "submit" and "search_customer_code" in e.fields)
    assert submit.action == "/societe/list.php" and submit.method in ("get", "post") and submit.value == "[REDACTED]"
    assert all(e.actor == "human" for e in events)
    assert TYPED not in json.dumps([e.model_dump(mode="json") for e in events])
    assert TYPED not in json.dumps([e.model_dump(mode="json") for e in log.entries])  # the query is never logged
    assert log.entries and not log.commit_hits
