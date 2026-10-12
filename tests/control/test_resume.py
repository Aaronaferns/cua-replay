"""The resume rules: the matrix, the once-per-handoff count, human-action classification, and mark-complete."""

from __future__ import annotations

from pathlib import Path

import pytest

from cua.artifact.lock import load_profile
from cua.control.capture import CaptureEvent, RequestLogEntry
from cua.control.resume import (
    HandoffRecord,
    ResumeState,
    decide_mark_complete,
    decide_resume,
    is_commit_control,
)

PROFILE = load_profile(Path(__file__).resolve().parents[2] / "apps" / "dolibarr" / "profile.json")


def ev(**fields) -> CaptureEvent:
    return CaptureEvent.model_validate(fields)


LOGIN_SUBMIT = ev(kind="submit", method="post", action="/index.php", fields=["username", "password", "code"])
LOGIN_CLICK = ev(kind="click", tag="input", type="submit", text="Connection", path="/index.php")
PAY_SUBMIT = ev(kind="submit", method="post", action="/compta/paiement.php",
                fields=["action", "confirm", "amount", "token"])
VALIDATE_CLICK = ev(kind="click", tag="button", text="Validate", path="/compta/facture/card.php")


def hit(commit: bool) -> RequestLogEntry:
    return RequestLogEntry(method="POST", url="http://x/compta/paiement.php", resource_type="document", commit=commit)


# ------------------------------------------------------------------------------------------------ the matrix


@pytest.mark.parametrize(
    ("state", "allowed", "reason"),
    [
        (ResumeState(), True, "ok"),
        (ResumeState(high_water_effect="read"), True, "ok"),
        (ResumeState(high_water_effect="read", commit_attempted=True), False, "commit_attempted_by_automation"),
        (ResumeState(high_water_effect="read", human_commit_clicks=1), False, "human_commit_click"),
        (ResumeState(high_water_effect="read", request_log_commit_hits=1), False, "request_log_commit"),
        (ResumeState(high_water_effect="commit"), False, "effect_above_read"),
        (ResumeState(high_water_effect="read", resumes_used=1), False, "already_resumed"),
        # several reasons at once: the commit evidence is named first
        (ResumeState(high_water_effect="commit", commit_attempted=True, resumes_used=1), False,
         "commit_attempted_by_automation"),
        (ResumeState(high_water_effect="commit", request_log_commit_hits=2, resumes_used=1), False,
         "request_log_commit"),
    ],
)
def test_resume_matrix(state, allowed, reason):
    decision = decide_resume(state)
    assert (decision.allowed, decision.reason) == (allowed, reason)
    assert decision.message


def test_clean_read_only_handoff_resumes_once_and_only_once():
    record = HandoffRecord("int-1", high_water_effect="read", profile=PROFILE)
    assert record.decide().allowed
    assert record.consume_resume().allowed and record.resumes == 1
    second = record.consume_resume()
    assert not second.allowed and second.reason == "already_resumed" and record.resumes == 1


def test_a_refused_resume_is_not_counted():
    record = HandoffRecord("int-1", high_water_effect="read", commit_attempted=True, profile=PROFILE)
    assert not record.consume_resume().allowed and record.resumes == 0


def test_automation_commit_marker_forbids_resume():
    record = HandoffRecord("int-1", profile=PROFILE)
    record.note_automation_commit()
    decision = record.decide()
    assert not decision.allowed and decision.reason == "commit_attempted_by_automation"


def test_human_commit_click_forbids_resume_but_the_login_does_not():
    record = HandoffRecord("int-1", high_water_effect="read", profile=PROFILE)
    assert not record.note_human_action(LOGIN_SUBMIT) and not record.note_human_action(LOGIN_CLICK)
    assert record.decide().allowed
    assert record.note_human_action(PAY_SUBMIT)
    assert record.decide().reason == "human_commit_click"


def test_a_human_click_on_a_commit_labelled_control_forbids_resume():
    record = HandoffRecord("int-1", high_water_effect="read", profile=PROFILE)
    assert record.note_human_action(VALIDATE_CLICK)
    assert record.decide().reason == "human_commit_click"


def test_request_log_commit_hit_forbids_resume():
    record = HandoffRecord("int-1", high_water_effect="read", profile=PROFILE)
    assert not record.note_request(hit(False)) and record.decide().allowed
    assert record.note_request(hit(True))
    assert record.decide().reason == "request_log_commit"


def test_high_water_above_read_forbids_resume_and_never_comes_back_down():
    record = HandoffRecord("int-1", high_water_effect="read", profile=PROFILE)
    record.note_effect("commit")
    record.note_effect("none")
    assert record.high_water_effect == "commit" and record.decide().reason == "effect_above_read"


# --------------------------------------------------------------------------------------- classification


def test_submit_classification_follows_the_profiles_commit_routes():
    # the payment form posts to a commit route and has the required body fields
    assert is_commit_control(PAY_SUBMIT, PROFILE)
    # same path, but the form lacks the fields the route requires: cannot match
    assert not is_commit_control(ev(kind="submit", method="post", action="/compta/paiement.php", fields=["amount"]),
                                 PROFILE)
    # a GET search form is not a validation commit, though the route path is "*"
    assert not is_commit_control(ev(kind="submit", method="get", action="/societe/list.php",
                                    fields=["search_name", "limit"]), PROFILE)
    # a GET form that does carry the route's query parameters is
    assert is_commit_control(ev(kind="submit", method="get", action="/compta/facture/card.php",
                                fields=["action", "confirm"]), PROFILE)


def test_unknown_profile_is_conservative_for_submits_only():
    assert is_commit_control(LOGIN_SUBMIT, None)
    assert not is_commit_control(ev(kind="change", tag="input", value="[REDACTED]"), None)
    assert not is_commit_control(ev(kind="enter", tag="input", name="code"), None)


def test_a_commit_word_inside_another_word_does_not_count():
    assert not is_commit_control(ev(kind="click", tag="a", text="Company overview"), PROFILE)
    assert is_commit_control(ev(kind="click", tag="a", text="Confirm payment"), PROFILE)


# --------------------------------------------------------------------------------------------- mark-complete


async def test_mark_complete_requires_checkpoint():
    extracted: list[str] = []

    def outputs():
        extracted.append("called")
        return {"amount": "12.50"}

    refused = await decide_mark_complete(checkpoint=lambda: False, extract_outputs=outputs)
    assert not refused.allowed and refused.reason == "checkpoint_not_satisfied" and refused.outputs is None
    assert extracted == []  # outputs are not even read from a page that is not at the success checkpoint

    assert (await decide_mark_complete(checkpoint=None, extract_outputs=outputs)).reason == "no_checkpoint_available"
    assert (await decide_mark_complete(checkpoint=lambda: True, extract_outputs=None)).reason == "no_checkpoint_available"

    allowed = await decide_mark_complete(checkpoint=lambda: True, extract_outputs=outputs)
    assert allowed.allowed and allowed.reason == "ok" and allowed.outputs == {"amount": "12.50"}


async def test_mark_complete_needs_outputs_too_and_accepts_async_callbacks():
    async def yes():
        return True

    async def nothing():
        return None

    async def some():
        return {"outstanding": "1,000.00"}

    assert (await decide_mark_complete(checkpoint=yes, extract_outputs=nothing)).reason == "outputs_not_extractable"
    done = await decide_mark_complete(checkpoint=yes, extract_outputs=some)
    assert done.allowed and done.outputs == {"outstanding": "1,000.00"}


async def test_a_raising_callback_refuses_without_leaking_its_message():
    def boom():
        raise RuntimeError("page said SECRET-TEXT")

    for decision in (
        await decide_mark_complete(checkpoint=boom, extract_outputs=lambda: {}),
        await decide_mark_complete(checkpoint=lambda: True, extract_outputs=boom),
    ):
        assert not decision.allowed and "SECRET-TEXT" not in repr(decision) and "SECRET-TEXT" not in decision.message
