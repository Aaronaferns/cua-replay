"""The navigation fence: after an action that navigates, replay waits for the surface's settle before it classifies."""

from __future__ import annotations

from fakes import BASE, AllowGuard, Clock, FakeDolibarr, lease
from sample_docs import capability, dolibarr_profile

from cua.replay.interpreter import replay

PROFILE = dolibarr_profile()


async def _run(fake: FakeDolibarr):
    clock = Clock()
    result = await replay(capability(), PROFILE, {"customer_code": "CU1212-0005"}, surface=fake, guard=fake.guard,
                          lease=lease, base_url=BASE, run_id="run-fence", now_ms=clock.now, sleep=clock.sleep)
    return result


async def test_the_fence_is_passed_once_per_navigating_action():
    fake = FakeDolibarr(AllowGuard())
    cap = capability()
    result = await _run(fake)
    assert result.status == "success"
    navigating = sum(1 for s in cap.steps if s.navigates)
    assert fake.log.count("settle") == 1 + navigating  # the entry navigation plus each navigating step


async def test_the_fence_comes_before_the_first_classification_of_a_navigation():
    fake = FakeDolibarr(AllowGuard())
    await _run(fake)
    first_nav = fake.log.index("navigate")
    assert fake.log[first_nav + 1] == "settle"


async def test_a_screen_that_never_settles_is_judged_by_the_classifier_not_the_fence():
    fake = FakeDolibarr(AllowGuard())
    fake.delay_polls = 10_000  # the fence reports "not settled", then the classifier sees an unknown screen
    result = await _run(fake)
    assert result.status == "failure" and result.failure is not None and result.failure.code == "UNKNOWN_STATE"
