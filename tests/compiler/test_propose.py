"""Template-proposed observed states, their collision check against the catalog, and the S5 survival measure."""

from __future__ import annotations

from pathlib import Path

import pytest
from trace_builder import CARD, LIST, build_trace, screen

from cua.artifact.lock import load_profile
from cua.artifact.predicates import AllOf, UrlMatches
from cua.artifact.propose import survival_edits, template_signature, url_glob
from cua.artifact.screen import view_from_outline
from cua.artifact.validate import require_valid
from cua.compiler.compile import CompileError, compile_proposing, compile_trace, template_signatures

ROOT = Path(__file__).resolve().parents[2]
PROFILE = load_profile(ROOT / "apps" / "dolibarr" / "profile.json")
KW = dict(name="lookup_customer_outstanding", discovered_by="alice")


def without(*state_ids: str):
    return PROFILE.model_copy(update={"states": {k: v for k, v in PROFILE.states.items() if k not in state_ids}})


def test_unknown_screen_is_refused_without_proposals_and_proposed_with_them():
    profile = without("customer_card")
    with pytest.raises(CompileError):
        compile_trace(build_trace(), profile, **KW)
    cap, extended = compile_proposing(build_trace(), profile, **KW)
    proposed = {sid: st for sid, st in extended.states.items() if sid not in profile.states}
    assert list(proposed) == ["societe_card"]
    state = proposed["societe_card"]
    assert state.origin == "observed" and not state.edited and state.layer == "page"
    assert state.signature == UrlMatches(pattern="/societe/card.php*")  # Dolibarr pages carry no heading
    assert cap.steps[-1].expect == "societe_card" and cap.success.state == "societe_card"
    require_valid(cap, extended)


def test_a_heading_joins_the_url_glob_in_the_signature():
    url, title, outline = CARD
    heading_card = screen((url, title, '- heading "Third party card" [ref=e0]\n' + outline), 4).model_copy(
        update={"heading": "Third party card"})
    trace = build_trace()
    actions = [a.model_copy(update={k: heading_card for k in ("pre", "post") if getattr(a, k) is not None
                                    and getattr(a, k).url.endswith("socid=*")}) for a in trace.actions]
    _, extended = compile_proposing(build_trace(actions=actions), without("customer_card"), **KW)
    sig = extended.states["societe_card"].signature
    assert isinstance(sig, AllOf) and [type(c).__name__ for c in sig.of] == ["UrlMatches", "Text"]
    assert sig.of[1].equals == "Third party card" and sig.of[1].role == "heading"


def test_a_heading_that_echoes_the_input_or_carries_data_is_left_out():
    for heading in ("CU1910-00021", "Balance 199.75"):
        card = screen(CARD, 4).model_copy(update={"heading": heading})
        trace = build_trace()
        actions = [a.model_copy(update={k: card for k in ("pre", "post") if getattr(a, k) is not None
                                        and getattr(a, k).url.endswith("socid=*")}) for a in trace.actions]
        _, extended = compile_proposing(build_trace(actions=actions), without("customer_card"), **KW)
        assert extended.states["societe_card"].signature == UrlMatches(pattern="/societe/card.php*")


def test_a_proposal_that_overlaps_a_known_state_is_a_collision():
    # the unfiltered list screen is not recognised (no column header), the filtered one is: same URL glob
    odd_list = ("/societe/list.php?leftmenu=customers&type=c", "Customers", "- generic [ref=e1]: nothing recognisable\n")
    trace = build_trace()
    first = trace.actions[0].model_copy(update={"post": screen(odd_list, 2)})
    second = trace.actions[1].model_copy(update={"pre": screen(odd_list, 2), "post": screen(odd_list, 2)})
    third = trace.actions[2].model_copy(update={"pre": screen(odd_list, 2)})
    odd = build_trace(actions=[first, second, third, *trace.actions[3:]])
    with pytest.raises(CompileError) as e:
        compile_proposing(odd, PROFILE, **KW)
    assert any("proposed states collide" in p and "/societe/list.php*" in p for p in e.value.problems)


def test_a_known_profile_proposes_nothing():
    cap, extended = compile_proposing(build_trace(), PROFILE, **KW)
    assert extended == PROFILE
    assert cap == compile_trace(build_trace(), PROFILE, **KW)


def test_template_signatures_one_per_distinct_screen():
    templates = template_signatures(build_trace(), PROFILE)
    assert sorted(templates) == ["customer_card", "customer_list", "home"]
    assert templates["customer_list"].signature == template_signature("/societe/list.php?x=1", None)
    assert url_glob("http://h:1/comm/card.php?socid=63") == "/comm/card.php*"


def test_survival_counts_the_templates_a_different_run_would_need_edited():
    templates = template_signatures(build_trace(), PROFILE)
    ok = [(view_from_outline("http://h/index.php?mainmenu=home", "- generic: Global view\n"), "home"),
          (view_from_outline("http://h/societe/list.php?search=zz", LIST[2]), "customer_list"),
          (view_from_outline("http://h/societe/card.php?socid=9", CARD[2]), "customer_card")]
    assert survival_edits(templates, ok) == []
    moved = [(view_from_outline("http://h/societe/other.php?socid=9", CARD[2]), "customer_card")]
    assert survival_edits(templates, moved) == ["customer_card"]
    clash = {**templates, "also_list": templates["customer_list"]}
    assert sorted(survival_edits(clash, ok[1:2])) == ["also_list", "customer_list"]
