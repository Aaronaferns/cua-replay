"""The runtime session against the real Dolibarr target: it opens and is logged in."""

from __future__ import annotations

import pytest

from cua.control.runtime import open_session

pytestmark = pytest.mark.live


async def test_open_session_is_logged_in_on_the_real_target(tmp_path):
    async with open_session(run_id="rt-live", evidence_root=tmp_path) as session:
        obs = await session.surface.observe(screenshot=False)
        assert session.surface.page.url.startswith(session.base_url)
        assert "Login" not in obs.facts.title
        assert not await session.surface.find(
            _login_password_strategy(session)
        ), "the login form must be gone after a scripted login"


def _login_password_strategy(session):
    from cua.artifact.schema import Strategy

    return Strategy(kind="role", role="textbox", name="Password", why="live check", matched_at_record="test")
