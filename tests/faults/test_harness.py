"""The fault harness against real Chromium and local servers (offline). The server hit log is the oracle for 'did the
request reach the app'; the guard's `on_block` list is the oracle for 'did the guard still see it'."""

from __future__ import annotations

import ast
import contextlib
import time
from pathlib import Path

import pytest
from playwright.async_api import Browser
from pydantic import ValidationError

from cua.faults.harness import (
    AppErrorFault,
    FaultEvent,
    InterstitialFault,
    LostResponseFault,
    SlowFault,
    UnknownDialogFault,
    install_faults,
    parse_fault,
)
from cua.safety.network import BlockedRequest, new_guarded_context
from cua.safety.policy import Policy, PolicyDenied, load_policy

FORM = "action=confirm_paiement&confirm=yes&amount=10"
POST_JS = (
    "([url, body]) => fetch(url, {method: 'POST', headers: {'content-type': 'application/x-www-form-urlencoded'}, "
    "body}).then(r => r.status).catch(() => 'blocked')"
)
GET_JS = "(url) => fetch(url).then(r => r.status).catch(() => 'blocked')"


def policy_for(*origins: str, sandbox: bool = True, deny: list[str] | None = None) -> Policy:
    return Policy.model_validate({
        "version": "cua.policy/v1", "name": "fault-test", "sandbox": sandbox, "origins": list(origins),
        "routes": {"allow": ["/**"], "deny": deny or []}, "actions": ["click", "navigate", "extract"],
        "max_effect": "commit",
    })


@pytest.fixture
async def world(browser: Browser, servers):
    app, other = servers(), servers()
    app.serve("/", "<html><head><title>App</title></head><body><h1>app</h1></body></html>")
    app.serve("/data.json", '{"ok": true}', ctype="application/json")
    blocked: list[BlockedRequest] = []
    fired: list[FaultEvent] = []
    policy = policy_for(app.origin, deny=["/secret/**"])
    context = await new_guarded_context(browser, policy, on_block=blocked.append)
    page = await context.new_page()
    yield app, other, context, page, policy, blocked, fired
    with contextlib.suppress(Exception):
        await context.close()


# -------------------------------------------------------------------------------------------------- installation


async def test_non_sandbox_policy_refuses_installation(browser, servers):
    app = servers()
    policy = policy_for(app.origin, sandbox=False)
    context = await new_guarded_context(browser, policy, on_block=lambda b: None)
    with pytest.raises(PolicyDenied) as denied:
        await install_faults(context, policy, [AppErrorFault(match="/**")], on_fault=print)
    assert denied.value.decision.rule == "sandbox_only"
    page = await context.new_page()  # nothing was registered: the app is untouched
    assert (await page.goto(app.origin + "/")).status == 200
    await context.close()


def test_specs_are_closed_and_strict():
    with pytest.raises(ValidationError):
        AppErrorFault(match="/x", surprise=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        parse_fault("meltdown", match="/x")
    with pytest.raises(ValidationError):
        AppErrorFault(match="/x", times=0)
    with pytest.raises(ValueError):
        parse_fault("slow", match="/x")
    with pytest.raises(ValueError):
        parse_fault("app_error:5", match="/x")
    slow = parse_fault("slow:250", match="/societe/**", times=2)
    assert isinstance(slow, SlowFault) and slow.ms == 250 and slow.times == 2
    assert parse_fault("lost_response_after_commit", match="/p").methods == ["POST"]


def test_faults_import_only_playwright_pydantic_and_the_policy_type():
    root = Path(__file__).resolve().parents[2] / "src" / "cua" / "faults"
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                modules = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                modules = [node.module]
            else:
                modules = []
            for module in modules:
                if module.split(".")[0] == "cua":
                    assert module == "cua.safety.policy", (path.name, module)
                assert module.split(".")[0] not in {"anthropic", "fastapi", "uvicorn"}, (path.name, module)


# ----------------------------------------------------------------------------------------------------- each fault


async def test_slow_delays_matching_requests_only(world):
    app, _, context, page, policy, blocked, fired = world
    await page.goto(app.origin + "/")
    await install_faults(context, policy, [SlowFault(match="/slow/**", ms=400)], on_fault=fired.append)

    t0 = time.monotonic()
    assert await page.evaluate(GET_JS, "/slow/a") == 200
    slow = time.monotonic() - t0
    t0 = time.monotonic()
    assert await page.evaluate(GET_JS, "/fast/a") == 200
    fast = time.monotonic() - t0

    assert slow >= 0.38 and fast < 0.3
    assert app.count("/slow/a") == 1  # delayed, then really served by the guard's own route
    assert [(e.kind, e.url, e.detail) for e in fired] == [("slow", app.origin + "/slow/a", {"ms": 400})]
    assert all(e.declared for e in fired) and blocked == []


async def test_app_error_fulfils_a_500_without_reaching_the_app(world):
    app, _, context, page, policy, blocked, fired = world
    await page.goto(app.origin + "/")
    await install_faults(context, policy, [AppErrorFault(match="/broken")], on_fault=fired.append)

    response = await page.goto(app.origin + "/broken?x=1")
    assert response.status == 500 and "Injected fault" in await page.content()
    assert app.count("/broken") == 0
    assert fired[0].kind == "app_error" and fired[0].url == app.origin + "/broken"  # no query in the event
    # a non-matching request passes through the guard unchanged
    assert (await page.goto(app.origin + "/fine")).status == 200
    assert app.count("/fine") == 1 and len(fired) == 1


async def test_interstitial_injects_a_known_modal_into_the_real_page(world):
    app, _, context, page, policy, blocked, fired = world
    await install_faults(
        context, policy, [InterstitialFault(match="/", title="Cookie notice", button="Accept")], on_fault=fired.append
    )
    await page.goto(app.origin + "/")
    assert await page.inner_text("h1") == "app"  # the real page is still there
    dialog = page.get_by_role("dialog", name="Cookie notice")
    assert await dialog.is_visible()
    assert app.count("/") == 1 and [e.kind for e in fired] == ["interstitial"]
    await dialog.get_by_role("button", name="Accept").click()
    assert await page.get_by_role("dialog").count() == 0

    # sub-resource requests are never modified, and do not count as firings
    assert await page.evaluate("() => fetch('/').then(r => r.text())") == app.pages["/"][1]
    assert len(fired) == 1


async def test_interstitial_leaves_non_html_documents_alone(world):
    app, _, context, page, policy, blocked, fired = world
    await install_faults(context, policy, [InterstitialFault(match="/data.json", times=1)], on_fault=fired.append)
    await page.goto(app.origin + "/data.json")
    assert "ok" in await page.inner_text("body") and fired == []
    assert app.count("/data.json") == 1


async def test_unknown_dialog_is_a_different_modal(world):
    app, _, context, page, policy, blocked, fired = world
    await install_faults(context, policy, [UnknownDialogFault(match="/")], on_fault=fired.append)
    await page.goto(app.origin + "/")
    assert await page.inner_text("h1") == "app"
    assert await page.locator("#cua-fault-unknown-dialog").is_visible()
    assert await page.locator("#cua-fault-interstitial").count() == 0
    assert await page.get_by_role("button", name="Take the survey").is_visible()
    assert fired[0].kind == "unknown_dialog"


async def test_lost_response_sends_the_real_request_then_tells_the_browser_504(world):
    app, _, context, page, policy, blocked, fired = world
    await page.goto(app.origin + "/")
    spec = LostResponseFault(
        match=app.origin + "/pay.php", body_fields={"action": "confirm_paiement", "confirm": "yes"}
    )  # a full-URL glob this time
    await install_faults(context, policy, [spec], on_fault=fired.append)

    assert await page.evaluate(POST_JS, ["/pay.php", FORM]) == 504
    assert app.count("/pay.php") == 1 and app.bodies[-1] == FORM  # the commit reached the server exactly once
    assert fired[0].kind == "lost_response_after_commit" and fired[0].detail == {"status": 504, "server_status": 200}

    # other bodies, other methods: the guard passes them through untouched
    assert await page.evaluate(POST_JS, ["/pay.php", "action=add_paiement&confirm=yes"]) == 200
    assert await page.evaluate(GET_JS, "/pay.php") == 200
    assert app.count("/pay.php") == 3 and len(fired) == 1


# --------------------------------------------------------------------------------------- cooperation with the guard


async def test_off_allowlist_urls_stay_blocked_even_when_a_pattern_matches(world):
    app, other, context, page, policy, blocked, fired = world
    await page.goto(app.origin + "/")
    specs = [
        AppErrorFault(match="/**"),
        LostResponseFault(match=other.origin + "/**"),
        InterstitialFault(match="**"),
        SlowFault(match="**", ms=1),
    ]
    await install_faults(context, policy, specs, on_fault=fired.append)

    assert await page.evaluate(GET_JS, other.origin + "/x") == "blocked"  # app_error did not turn it into a 500
    assert await page.evaluate(POST_JS, [other.origin + "/pay.php", FORM]) == "blocked"
    assert other.hits == []
    assert {b.rule for b in blocked} == {"origin_allowlist"} and len(blocked) == 2
    assert not [e for e in fired if not e.url.startswith(app.origin)]  # nothing fired for the foreign origin

    # a path the policy denies on the allowed origin: the guard reports it; no fault fires on it
    before = len(fired)
    assert await page.evaluate(GET_JS, "/secret/data") == "blocked"
    assert app.count("/secret") == 0 and len(fired) == before
    assert blocked[-1].rule == "path_rules"


async def test_times_limits_are_honoured(world):
    app, _, context, page, policy, blocked, fired = world
    await page.goto(app.origin + "/")
    install = await install_faults(
        context, policy, [AppErrorFault(match="/flaky", times=2), SlowFault(match="/x", ms=1, times=1)],
        on_fault=fired.append,
    )
    statuses = [await page.evaluate(GET_JS, "/flaky") for _ in range(4)]
    assert statuses == [500, 500, 200, 200]
    assert app.count("/flaky") == 2
    assert install.count(0) == 2 and [e.fired for e in fired] == [1, 2]
    for _ in range(3):
        await page.evaluate(GET_JS, "/x")
    assert install.count(1) == 1


async def test_faults_compose_and_uninstall(world):
    app, _, context, page, policy, blocked, fired = world
    install = await install_faults(
        context, policy, [SlowFault(match="/", ms=100), InterstitialFault(match="/")], on_fault=fired.append
    )
    await page.goto(app.origin + "/")
    assert await page.locator("#cua-fault-interstitial").is_visible()
    assert sorted(e.kind for e in fired) == ["interstitial", "slow"]

    await install.uninstall()
    await page.goto(app.origin + "/")
    assert await page.locator("#cua-fault-interstitial").count() == 0 and len(fired) == 2


async def test_a_failing_event_callback_never_changes_what_the_browser_sees(world):
    app, _, context, page, policy, blocked, fired = world

    def explode(event: FaultEvent) -> None:
        raise RuntimeError("evidence store is down")

    await page.goto(app.origin + "/")
    await install_faults(context, policy, [AppErrorFault(match="/e")], on_fault=explode)
    assert await page.evaluate(GET_JS, "/e") == 500


def test_commit_policy_differs_from_the_default_sandbox_policy_only_in_name_and_ceiling():
    variables = {"DOLIBARR_PORT": "8087"}
    default = load_policy("dolibarr-sandbox", variables=variables)
    commit = load_policy("dolibarr-sandbox-commit", variables=variables)
    assert default.max_effect == "read" and commit.max_effect == "commit" and commit.sandbox
    assert commit.model_copy(update={"name": default.name, "max_effect": "read"}) == default
