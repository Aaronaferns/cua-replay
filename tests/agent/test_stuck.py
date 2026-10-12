"""The screen signature includes field values; the detector flags repeats, not progress."""

from __future__ import annotations

import pytest

from cua.agent.stuck import StuckDetector, screen_signature
from cua.surface.base import Observation, PageFacts

pytestmark = pytest.mark.asyncio(loop_scope="module")


def obs(snapshot, url="http://x/y", dialog=None):
    return Observation(epoch=1, facts=PageFacts(url=url, title="t"), snapshot=snapshot, dialog_snapshot=dialog)


def test_refs_do_not_change_the_signature_but_content_does():
    a = obs('- textbox "Name" [ref=e1]')
    assert screen_signature(a) == screen_signature(obs('- textbox "Name" [ref=e99]'))
    assert screen_signature(a) != screen_signature(obs('- textbox "Name" [ref=e1]: typed'))
    assert screen_signature(a) != screen_signature(obs('- textbox "Name" [ref=e1]', url="http://x/z"))
    assert screen_signature(a) != screen_signature(obs('- textbox "Name" [ref=e1]', dialog='- dialog "Sure?"'))


async def test_typing_into_a_field_changes_the_signature(fake_surface):
    surface, page, _ = fake_surface
    before = screen_signature(await surface.observe(screenshot=False))
    await page.fill("input[name=search_customer_code]", "CU1212-0005")
    after_obs = await surface.observe(screenshot=False)
    assert "CU1212-0005" in after_obs.snapshot  # the AI snapshot shows the textbox value inline
    assert screen_signature(after_obs) != before


def test_detector_flags_the_same_action_on_the_same_screen():
    d = StuckDetector(repeat_limit=3)
    assert d.check("s1", "click:e1") is None
    assert d.check("s2", "click:e1") is None  # same action, different screen: progress
    assert d.check("s1", "click:e1") is None
    assert "same action" in d.check("s1", "click:e1")


def test_detector_flags_a_screen_that_never_changes():
    d = StuckDetector(unchanged_limit=4)
    results = [d.check("s", f"press:{i}") for i in range(4)]
    assert results[:3] == [None, None, None] and "did not change" in results[3]
