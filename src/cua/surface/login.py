"""Scripted login: the harness fills the login form, a model never sees the credentials.

The form's controls are given as plain role + name targets (the app profile owns them; the caller wires them in).
Credentials come from `cua.config.settings()` as `SecretStr` and are unwrapped only at the moment they are typed.
They are never logged, never put in an exception message, and never part of an `ActionRequest`.

A login CAPTCHA (`textbox "Security code"`) is a human-only gate: it is detected before anything is typed and
reported as `CaptchaRequired`, never attempted.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, SecretStr

from cua.artifact.schema import Strategy
from cua.config import Settings, settings
from cua.control.lease import LeaseToken
from cua.safety.policy import ActionKind, ActionRequest
from cua.surface.base import NotUniqueError, SurfaceError
from cua.surface.web import WebElement, WebSurface

CAPTCHA_FIELD = "Security code"


class LoginError(SurfaceError):
    """The scripted login did not complete. Messages never contain credentials."""


class CaptchaRequired(LoginError):
    """The login form asks for a security code: only a human can continue."""


class LoginFailed(LoginError):
    """The login form is still shown after submitting."""


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    role: str
    name: str


class LoginTargets(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    username: Target
    password: Target
    submit: Target


def _strategy(target: Target, why: str) -> Strategy:
    return Strategy(kind="role", role=target.role, name=target.name, why=why, matched_at_record="login form")


async def _one(surface: WebSurface, target: Target, what: str) -> WebElement:
    found = await surface.find(_strategy(target, f"scripted login: {what}"))
    if len(found) != 1:
        # `find()` never resolves to 'the first': a login form with a missing or duplicated control is an error
        raise (NotUniqueError if found else LoginError)(f"login {what} control matched {len(found)} elements")
    return found[0]


async def has_login_form(surface: WebSurface, targets: LoginTargets) -> bool:
    return bool(await surface.find(_strategy(targets.password, "is the login form shown")))


async def scripted_login(
    surface: WebSurface,
    targets: LoginTargets,
    lease: LeaseToken,
    *,
    login: str | None = None,
    password: SecretStr | None = None,
    config: Settings | None = None,
) -> None:
    """Fill username and password and submit. Defaults to the configured run account (`CUA_RUN_LOGIN`,
    `CUA_RUN_PASSWORD`); pass `login` / `password` for another account (for example the no-rights user).
    Every UI action goes through the surface and therefore through the guard."""
    cfg = config or settings()
    login = login if login is not None else cfg.cua_run_login
    secret = password if password is not None else cfg.cua_run_password
    if secret is None:
        raise LoginError("no password is configured for the login account")

    await surface.observe(screenshot=False)  # let the page settle before looking for controls
    if await surface.find(_strategy(Target(role="textbox", name=CAPTCHA_FIELD), "login CAPTCHA check")):
        raise CaptchaRequired("the login form asks for a security code; a human must complete the login")

    def request(kind: ActionKind) -> ActionRequest:
        return ActionRequest(kind=kind, actor="handler", page_url=surface.page.url, effect="none", lease=lease)

    username_box = await _one(surface, targets.username, "username")
    password_box = await _one(surface, targets.password, "password")
    submit = await _one(surface, targets.submit, "submit")
    await surface.type(username_box, login, request("type"))
    await surface.type(password_box, secret.get_secret_value(), request("type"))
    await surface.click(submit, request("click"))

    await surface.observe(screenshot=False)
    if await surface.find(_strategy(Target(role="textbox", name=CAPTCHA_FIELD), "login CAPTCHA check")):
        raise CaptchaRequired("the login form asks for a security code; a human must complete the login")
    if await has_login_form(surface, targets):
        raise LoginFailed("the login form is still shown after submitting")
