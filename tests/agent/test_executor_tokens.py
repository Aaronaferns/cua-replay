"""Input tokens: the model writes `{{name}}`, the executor substitutes; raw values and control characters never pass."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import pytest

from cua.agent.actions import (
    Click,
    Done,
    Extract,
    Navigate,
    Press,
    RequestApproval,
    RequestHelp,
    Select,
    Type,
)
from cua.agent.executor import Executor, Rejected
from cua.artifact.lock import load_profile

CODE = "CU2601-00901"
PROFILE = load_profile(Path(__file__).resolve().parents[2] / "apps" / "dolibarr" / "profile.json")


class _Untouchable:
    """A surface / recorder / guard that fails the test if the executor reaches for it."""

    def __getattr__(self, name):
        raise AssertionError(f"the UI was touched ({name}) by an action that should have been rejected")


def executor() -> Executor:
    u = _Untouchable()
    return Executor(surface=u, guard=u, lease=u, recorder=u, profile=PROFILE,  # type: ignore[arg-type]
                    inputs={"customer_code": CODE}, outputs={"outstanding_amount": object()})


R = "ok"
RAW_IN_EVERY_STRING_ARGUMENT = [
    Click(rationale=f"open {CODE}", ref="e1"),
    Click(rationale=R, ref=CODE),
    Type(rationale=R, ref="e1", text=CODE),
    Type(rationale=R, ref="e1", text=f"x {CODE.lower()} y"),
    Type(rationale=f"typing {CODE}", ref="e1", text="{{customer_code}}"),
    Select(rationale=R, ref="e1", option=CODE),
    Press(rationale=CODE, key="Enter"),
    Navigate(rationale=R, url=f"http://localhost:8085/societe/list.php?search_customer_code={CODE}"),
    Navigate(rationale=R, url=f"http://localhost:8085/x?q={quote(CODE, safe='')}"),
    Extract(rationale=R, name=CODE, ref="e1"),
    Extract(rationale=R, name="outstanding_amount", ref=CODE),
    RequestApproval(rationale=R, ref="e1", summary=f"pay for {CODE}"),
    RequestHelp(rationale=R, reason=f"cannot find {CODE}"),
    Done(rationale=R, evidence_refs=["e1", CODE]),
]


@pytest.mark.parametrize("action", RAW_IN_EVERY_STRING_ARGUMENT, ids=lambda a: a.kind)
async def test_tokens_reject_raw_values_all_args(action):
    ex = executor()
    with pytest.raises(Rejected):
        ex.check_text(action)
    outcome = await ex.execute(action, obs=None)  # type: ignore[arg-type]
    assert outcome.status == "error" and "Rejected" in outcome.message
    assert CODE not in outcome.message  # the refusal itself must not echo the value


@pytest.mark.parametrize("bad", ["a\x00b", "a\nb", "a\rb", "a\tb", "a\x1bb", "a\x7fb", "a\x85b", "a b"])
@pytest.mark.parametrize("make", [
    lambda s: Click(rationale=s, ref="e1"),
    lambda s: Type(rationale="r", ref="e1", text=s),
    lambda s: Navigate(rationale="r", url="http://localhost:8085/" + s),
    lambda s: Done(rationale="r", evidence_refs=[s]),
], ids=["rationale", "text", "url", "evidence_refs"])
def test_control_characters_are_rejected(make, bad):
    with pytest.raises(Rejected, match="control character"):
        executor().check_text(make(bad))


def test_tokens_are_accepted_and_substituted_in_the_executor():
    ex = executor()
    action = Type(rationale="Filter by {{customer_code}}", ref="e1", text="{{customer_code}}")
    assert ex.check_text(action) == ["customer_code"]
    assert ex.substitute(action.text) == CODE
    url = "http://localhost:8085/societe/list.php?search_customer_code={{customer_code}}"
    assert ex.substitute(url, url=True).endswith("search_customer_code=CU2601-00901")
    assert ex.substitute("a {{customer_code}} b", url=True) == f"a {quote(CODE, safe='')} b"


def test_undeclared_and_malformed_tokens_are_rejected():
    ex = executor()
    with pytest.raises(Rejected, match="undeclared"):
        ex.check_text(Type(rationale="r", ref="e1", text="{{other}}"))
    with pytest.raises(Rejected, match="malformed"):
        ex.check_text(Type(rationale="r", ref="e1", text="{{customer code}}"))
    with pytest.raises(Rejected, match="malformed"):
        ex.check_text(Click(rationale="r {{", ref="e1"))


def test_short_values_only_match_whole_arguments():
    u = _Untouchable()
    ex = Executor(surface=u, guard=u, lease=u, recorder=u, profile=PROFILE,  # type: ignore[arg-type]
                  inputs={"qty": "42"}, outputs={})
    ex.check_text(Click(rationale="open item 142 of the list", ref="e1"))  # '42' inside ordinary text is fine
    with pytest.raises(Rejected):
        ex.check_text(Type(rationale="r", ref="e1", text="42"))
