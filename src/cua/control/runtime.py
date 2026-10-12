"""The runtime session: the one place that wires a browser run together.

Owner: Agent C. `open_session` launches Chromium, creates the ONLY browser context through
`cua.safety.network.new_guarded_context`, builds the policy guard, the web surface and the evidence bundle, and logs
in with the scripted login. Agents, replay and tests receive a `Session` and never assemble these parts themselves,
so every run is contained by the same policy, logged to the same bundle and redacted by the same redactor.

Every request the network layer refuses becomes a `policy` evidence event (actor `system`).
The lease starts as `automation`, generation 0; the lease-transition API (M5) replaces `Session.lease`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from playwright.async_api import async_playwright

from cua.artifact.lock import load_profile
from cua.artifact.schema import Profile
from cua.artifact.schema import Target as ProfileTarget
from cua.config import REPO_ROOT, Settings
from cua.config import settings as load_settings
from cua.control.lease import AUTOMATION, LeaseToken
from cua.evidence.writer import RunBundleWriter
from cua.safety.network import BlockedRequest, new_guarded_context
from cua.safety.policy import ActionRequest, Policy, PolicyDecision, PolicyGuard, load_policy
from cua.surface.login import LoginError, LoginTargets, Target, scripted_login
from cua.surface.web import DOLIBARR_SCOPE, WebSurface

VIEWPORT = {"width": 1366, "height": 900}


@dataclass
class Session:
    surface: WebSurface
    guard: PolicyGuard
    lease: Callable[[], LeaseToken]
    evidence: RunBundleWriter
    policy: Policy
    profile: Profile
    settings: Settings
    base_url: str


def login_targets(profile: Profile) -> LoginTargets:
    """Each login control maps to the role and name of its primary `role` strategy."""

    def target(spec: ProfileTarget, what: str) -> Target:
        for strategy in spec.strategies:
            if strategy.kind == "role" and strategy.role and strategy.name:
                return Target(role=strategy.role, name=strategy.name)
        raise LoginError(f"the profile's login {what} has no role strategy to log in with")

    login = profile.login
    return LoginTargets(
        username=target(login.username, "username"),
        password=target(login.password, "password"),
        submit=target(login.submit, "submit"),
    )


def _resolve(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else REPO_ROOT / p


@asynccontextmanager
async def open_session(
    *,
    run_id: str,
    policy: str = "dolibarr-sandbox",
    profile: str = "apps/dolibarr/profile.json",
    headed: bool = False,
    login: bool = True,
    evidence_root: Path | None = None,
    actor: Literal["agent", "replay"] = "replay",
    config: Settings | None = None,
) -> AsyncIterator[Session]:
    """Open a contained, logged-in browser session and close everything on exit.

    `actor` is who the opening navigation is attributed to. `config` overrides the environment settings (tests).
    `CaptchaRequired` from the login propagates; the browser and the bundle are closed first.
    """
    cfg = config or load_settings()
    loaded_policy = load_policy(policy, variables=cfg.policy_variables())
    loaded_profile = load_profile(_resolve(profile))
    writer = RunBundleWriter(run_id, root=evidence_root)
    status = "closed"
    try:
        writer.add_secret_values(cfg.secret_values(), label="secret")

        def on_block(blocked: BlockedRequest) -> None:
            writer.emit(
                "policy",
                actor="system",
                policy=PolicyDecision(allowed=False, rule=blocked.rule, reason=blocked.reason),
                detail=blocked.model_dump(mode="json"),
            )

        lease_token = LeaseToken(holder=AUTOMATION, generation=0)

        def lease() -> LeaseToken:
            return lease_token

        guard = PolicyGuard(loaded_policy, lease)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=not headed)
            try:
                context = await new_guarded_context(browser, loaded_policy, on_block=on_block, viewport=VIEWPORT)
                page = await context.new_page()
                surface = WebSurface(page, guard, scope=DOLIBARR_SCOPE)
                session = Session(
                    surface=surface,
                    guard=guard,
                    lease=lease,
                    evidence=writer,
                    policy=loaded_policy,
                    profile=loaded_profile,
                    settings=cfg,
                    base_url=cfg.browser_base_url,
                )
                writer.emit("state", actor="system", detail={"session": "open", "policy": loaded_policy.name})
                if login:
                    url = cfg.browser_base_url + loaded_profile.entry.route
                    # the page is blank, so the action "happens" at its own destination (a blank page is not allowed)
                    request = ActionRequest(
                        kind="navigate", actor=actor, page_url=url, target_url=url, effect="none", lease=lease()
                    )
                    await surface.navigate(url, request)
                    await scripted_login(surface, login_targets(loaded_profile), lease(), config=cfg)
                    writer.emit("state", actor="system", detail={"session": "logged_in"})
                yield session
            finally:
                await browser.close()
    except BaseException:
        status = "aborted"
        raise
    finally:
        writer.close(status=status)
