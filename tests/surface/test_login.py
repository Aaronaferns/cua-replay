"""The scripted login on the offline login pages: secrets stay out of every message, a CAPTCHA is never attempted."""

from __future__ import annotations

import pytest
from fakesite import LEASE, FakeGuard

from cua.config import Settings
from cua.safety.policy import PolicyDenied
from cua.surface.login import CaptchaRequired, LoginFailed, LoginTargets, Target, scripted_login

pytestmark = pytest.mark.asyncio(loop_scope="module")

TARGETS = LoginTargets(username=Target(role="textbox", name="Login"), password=Target(role="textbox", name="Password"),
                       submit=Target(role="button", name="Login"))
PASSWORD = "pw-for-test"


def config(password: str = PASSWORD) -> Settings:
    return Settings(cua_run_login="tester", cua_run_password=password)


async def test_login_types_secrets_through_the_guard_and_submits(open_page, guard):
    surface, page = await open_page("/index.php")
    await scripted_login(surface, TARGETS, LEASE, config=config())
    assert (await surface.observe(screenshot=False)).facts.title == "Customers"
    assert [(r.kind, r.actor, r.effect) for r in guard.calls[-3:]] == [
        ("type", "handler", "none"), ("type", "handler", "none"), ("click", "handler", "none")]
    assert PASSWORD not in repr(guard.calls)


async def test_wrong_password_fails_without_echoing_it(open_page):
    surface, _ = await open_page("/index.php")
    with pytest.raises(LoginFailed) as e:
        await scripted_login(surface, TARGETS, LEASE, config=config("not-the-password"))
    assert "not-the-password" not in repr(e.value) and "tester" not in repr(e.value)


async def test_captcha_is_detected_before_anything_is_typed(open_page):
    guard = FakeGuard(allow=True)
    surface, page = await open_page("/index.php?captcha=1", guard_=guard)
    with pytest.raises(CaptchaRequired):
        await scripted_login(surface, TARGETS, LEASE, config=config())
    assert guard.calls == []
    assert await page.locator("input[name=username]").input_value() == ""


async def test_guard_denial_stops_the_login_before_the_ui_is_touched(open_page):
    deny = FakeGuard(allow=False)
    surface, page = await open_page("/index.php", guard_=deny)
    with pytest.raises(PolicyDenied):
        await scripted_login(surface, TARGETS, LEASE, config=config())
    assert len(deny.calls) == 1 and await page.locator("input[name=username]").input_value() == ""


async def test_missing_password_setting_is_a_typed_error(open_page):
    from cua.surface.login import LoginError

    surface, _ = await open_page("/index.php")
    with pytest.raises(LoginError):
        await scripted_login(surface, TARGETS, LEASE, config=Settings(cua_run_login="tester"))
