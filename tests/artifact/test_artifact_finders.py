"""Static validation: no recorded value (an input literal or personal data) survives into the artifact."""

from __future__ import annotations

import copy

import pytest
from sample_docs import capability_dict, dolibarr_profile

from cua.artifact.schema import Capability
from cua.artifact.validate import validate_capability

PROFILE = dolibarr_profile()


def problems_of(doc: dict) -> list[str]:
    return validate_capability(Capability.model_validate(doc), PROFILE)


def first_strategy(doc: dict) -> dict:
    return next(iter(doc["targets"].values()))["strategies"][0]


def test_the_sample_capability_is_clean():
    assert problems_of(capability_dict()) == []


@pytest.mark.parametrize("field,value", [
    ("name", "CU1212-0005"),  # an input value left in a role finder
    ("name", "Customer CU1910-00021 card"),  # embedded in a longer name
    ("name", "write to alice@example.com"),  # an email address
    ("name", "call +33 1 23 45 67 89"),  # a phone number
])
def test_finders_contain_no_input_or_pii(field, value):
    doc = capability_dict()
    strategy = next(s for t in doc["targets"].values() for s in t["strategies"] if s["kind"] == "role")
    strategy[field] = value
    problems = problems_of(doc)
    assert problems and any("finder" in p for p in problems)
    blob = " ".join(problems)
    assert value not in blob and "alice" not in blob and "1212" not in blob  # the value is never echoed


def test_a_lifted_input_in_a_finder_is_fine_and_a_literal_beside_it_is_not():
    doc = capability_dict()
    strategy = next(t for t in doc["targets"].values() if t["strategies"][0]["kind"] == "table_cell")["strategies"][0]
    strategy["row_key"] = "${customer_code}"
    assert problems_of(doc) == []
    strategy["row_key"] = "${customer_code} CU1212-0005"
    assert any("finder" in p for p in problems_of(doc))


def test_css_finders_are_checked_too():
    doc = copy.deepcopy(capability_dict())
    target = next(iter(doc["targets"].values()))
    target["strategies"].append({"kind": "css", "css": "tr#row-CU1212-0005 a", "why": "t", "matched_at_record": "1"})
    assert any("finder" in p for p in problems_of(doc))


def test_actions_and_predicates_must_not_hold_the_value_either():
    doc = capability_dict()
    nav = next(s for s in doc["steps"] if s["do"]["kind"] == "navigate")
    nav["do"]["route"] = "/societe/list.php?type=c&search_code=CU1212-0005"
    assert any("action" in p and "literal" in p for p in problems_of(doc))

    doc = capability_dict()
    doc["success"]["checks"] = [{"kind": "text", "equals": "Customer CU1212-0005"}]
    assert any("success.checks" in p for p in problems_of(doc))


def test_the_dolibarr_profile_and_its_own_finders_do_not_trip_the_scan():
    # labels such as "Customer Code" and "Third-party name" are not values
    assert problems_of(capability_dict()) == []
