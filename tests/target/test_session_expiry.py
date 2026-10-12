"""M4 "session expiry" on the real target: the session is deleted server-side and the next navigation, at the SAME url,
renders the login form, which the profile classifier labels `login`."""

from __future__ import annotations

import pytest
from livetarget import expire_session, reset_to_baseline

from cua.control.runtime import Session, open_session
from cua.replay.classifier import classify_observation
from cua.safety.policy import ActionRequest

pytestmark = pytest.mark.live

PATH = "/societe/list.php?type=c"


async def go(session: Session, url: str) -> None:
    request = ActionRequest(kind="navigate", actor="replay", page_url=session.surface.page.url, target_url=url,
                            effect="none", lease=session.lease())
    await session.surface.navigate(url, request)


async def screen_label(session: Session) -> str:
    obs = await session.surface.observe(screenshot=False, settle=True)
    return classify_observation(session.profile, obs).label


async def session_cookie(session: Session) -> str:
    cookies = await session.surface.page.context.cookies()
    ours = [c["value"] for c in cookies if c["name"].startswith("DOLSESSID_")]
    assert len(ours) == 1, "one Dolibarr session cookie after login"
    return ours[0]


@pytest.fixture(scope="module", autouse=True)
def baseline():
    return reset_to_baseline()


async def test_expiring_the_session_server_side_shows_the_login_form_at_the_same_url(tmp_path):
    async with open_session(run_id="m4-session-expiry", evidence_root=tmp_path) as session:
        url = session.base_url + PATH
        await go(session, url)
        assert await screen_label(session) == "customer_list"  # logged in, on a real page
        before = session.surface.page.url

        assert expire_session(await session_cookie(session)) == 1

        await go(session, url)
        url_after = session.surface.page.url
        assert url_after == before  # no redirect: the login form is rendered where the page was requested
        assert await screen_label(session) == "login"


async def test_expiring_all_sessions_of_the_run_user(tmp_path):
    async with open_session(run_id="m4-session-expiry-all", evidence_root=tmp_path) as session:
        url = session.base_url + PATH
        await go(session, url)
        assert await screen_label(session) == "customer_list"

        assert expire_session(None) >= 1  # at least this browser's session (others of the run user go too)

        await go(session, url)
        assert await screen_label(session) == "login"


def test_an_unknown_or_malformed_session_id_expires_nothing_and_is_not_injectable():
    assert expire_session("doesnotexist0123456789") == 0
    from dolictl import CtlError

    with pytest.raises(CtlError):
        expire_session("x; rm -rf /")
