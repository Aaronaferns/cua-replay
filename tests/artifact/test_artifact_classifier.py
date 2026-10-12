"""Classifier contract on the real profile and stored Dolibarr screens: specialization, ambiguity, near misses."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sample_docs import dolibarr_profile

from cua.artifact.classify import classify
from cua.artifact.predicates import ScreenView, holds
from cua.artifact.schema import State
from cua.artifact.screen import parse_outline, view_from_outline

OUTLINES = json.loads((Path(__file__).parent / "fixtures" / "dolibarr" / "outlines.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def profile():
    return dolibarr_profile()


def _view(name: str):
    return view_from_outline(OUTLINES[name]["url"], OUTLINES[name]["outline"])


def test_specializes_resolves_not_found(profile):
    """Both states match an empty search result; the specialization wins, so the list stays a list."""
    empty = _view("05_customer_list_none")
    parent_sig, child_sig = profile.states["customer_list"].signature, profile.states["customer_not_found"].signature
    assert holds(parent_sig, empty) and holds(child_sig, empty)  # a genuine overlap, legal only via `specializes`
    assert profile.states["customer_not_found"].specializes == "customer_list"
    c = classify(profile, empty)
    assert (c.kind, c.state) == ("state", "customer_not_found")
    for name in ("03_customer_list", "04_customer_list_alpha", "06_customer_list_nearmiss"):
        assert classify(profile, _view(name)).label == "customer_list"


def test_overlap_without_specializes_is_ambiguous_and_names_the_candidates(profile):
    states = dict(profile.states)
    states["list_twin"] = State(description="twin", signature=profile.states["customer_list"].signature,
                                origin="declared")
    twin = profile.model_copy(update={"states": states})
    c = classify(twin, _view("03_customer_list"))
    assert c.kind == "ambiguous" and c.state is None and c.label == "ambiguous"
    assert c.candidates == ("customer_list", "list_twin")


def test_ambiguous_state_is_failure_at_the_classifier(profile):
    """A specialization only shadows its own parent: a sibling that also matches is still an ambiguity."""
    states = dict(profile.states)
    states["not_found_twin"] = State(description="twin", signature=profile.states["customer_not_found"].signature,
                                     specializes="customer_list", origin="declared")
    twin = profile.model_copy(update={"states": states})
    c = classify(twin, _view("05_customer_list_none"))
    assert c.kind == "ambiguous" and c.candidates == ("customer_not_found", "not_found_twin")


def test_unknown_carries_near_misses_and_never_a_state(profile):
    view = view_from_outline("http://h/societe/list.php?type=c", '- generic [ref=e1]: Something else\n')
    c = classify(profile, view)
    assert c.kind == "unknown" and c.state is None
    assert 1 <= len(c.near_misses) <= 3 and c.near_misses[0].startswith("customer_list (")


def test_classification_is_pure_and_context_free(profile):
    views = [_view(n) for n in sorted(OUTLINES) if "dialog" not in OUTLINES[n]]
    first = [classify(profile, v) for v in views]
    assert [classify(profile, v) for v in reversed(views)] == list(reversed(first))
    assert [classify(profile, v) for v in views] == first


def test_an_open_dialog_is_classified_alone(profile):
    entry = OUTLINES["12_validate_dialog"]
    page = view_from_outline(entry["url"], entry["outline"])
    dialog = ScreenView(url=entry["url"], nodes=parse_outline(entry["dialog"]))
    assert classify(profile, page, dialog).label == "invoice_validate_confirm"
    nothing = ScreenView(url=entry["url"], nodes=())
    assert classify(profile, page, nothing).kind == "unknown"  # an unrecognised dialog hides the page

