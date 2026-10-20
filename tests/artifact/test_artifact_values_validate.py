"""Typed input checks, output parsing, and the static validation of a capability against its profile."""

from __future__ import annotations

import pytest
from sample_docs import capability, capability_dict, dolibarr_profile

from cua.artifact.schema import Capability, FieldSpec, NumberFormat
from cua.artifact.validate import (
    ArtifactInvalid,
    require_valid,
    validate_capability,
    validate_profile,
)
from cua.artifact.values import ValueRejected, check_input, parse_output

NBSP = " "


@pytest.mark.parametrize("spec,value", [
    (FieldSpec(type="string", pattern=r"CU\d{4}-\d{4,5}"), "CU1212-0005"),
    (FieldSpec(type="decimal"), "-75.37"),
    (FieldSpec(type="integer"), "42"),
    (FieldSpec(type="date"), "2026-10-12"),
])
def test_valid_inputs(spec, value):
    check_input(spec, value)


@pytest.mark.parametrize("spec,value", [
    (FieldSpec(type="string", pattern=r"CU\d{4}-\d{4,5}"), "CU12-5"),
    (FieldSpec(type="string", pattern=r"CU\d{4}-\d{4,5}"), "xCU1212-0005"),  # full match only
    (FieldSpec(type="string"), " padded"),
    (FieldSpec(type="string"), ""),
    (FieldSpec(type="string"), "a\nb"),
    (FieldSpec(type="decimal"), "75,37"),
    (FieldSpec(type="integer"), "4.2"),
    (FieldSpec(type="date"), "12/10/2026"),
])
def test_invalid_inputs_do_not_echo_the_value(spec, value):
    with pytest.raises(ValueRejected) as e:
        check_input(spec, value)
    assert value.strip() == "" or value not in str(e.value)


@pytest.mark.parametrize("shown,expected", [
    (f"75.37{NBSP}€", "75.37"),
    (" Current outstanding bill 75.37 €", "75.37"),
    ("1,234.50", "1234.50"),
    ("-3.10", "-3.10"),
    ("0", "0"),
])
def test_parse_decimal_output(shown, expected):
    assert parse_output(FieldSpec(type="decimal"), shown) == expected


def test_parse_decimal_with_european_format():
    fmt = NumberFormat(decimal=",", group=".")
    assert parse_output(FieldSpec(type="decimal"), "1.234,50 €", fmt) == "1234.50"
    fmt = NumberFormat(decimal=",", group=" ")
    assert parse_output(FieldSpec(type="decimal"), f"1{NBSP}234,5", fmt) == "1234.5"


@pytest.mark.parametrize("shown", ["", "no digits", "75.37 and 83.00", "1.2.3"])
def test_parse_output_rejects_ambiguous_or_missing_numbers(shown):
    with pytest.raises(ValueRejected):
        parse_output(FieldSpec(type="decimal"), shown)


def test_parse_other_types():
    assert parse_output(FieldSpec(type="integer"), "(13)") == "13"
    assert parse_output(FieldSpec(type="date"), " 2026-10-12 ") == "2026-10-12"
    assert parse_output(FieldSpec(type="string"), f"CU1212-0005 {NBSP} ") == "CU1212-0005"
    with pytest.raises(ValueRejected):
        parse_output(FieldSpec(type="string", pattern=r"CU\d+"), "XX")
    with pytest.raises(ValueRejected):
        parse_output(FieldSpec(type="date"), "tomorrow")


def test_number_format_separators_must_differ():
    with pytest.raises(ValueError):
        NumberFormat(decimal=".", group=".")


# --- static validation -----------------------------------------------------------------------------------------


def test_sample_documents_are_consistent():
    assert validate_profile(dolibarr_profile()) == []
    assert validate_capability(capability(), dolibarr_profile()) == []
    require_valid(capability(), dolibarr_profile())


def _problems(mutate) -> list[str]:
    doc = capability_dict()
    mutate(doc)
    return validate_capability(Capability.model_validate(doc), dolibarr_profile())


def test_unknown_references_are_reported():
    assert any("unknown state" in p for p in _problems(lambda d: d["steps"][1].update(expect="nowhere")))
    assert any("unknown target" in p for p in _problems(lambda d: d["steps"][1]["do"].update(target="ghost")))
    assert any("undeclared output" in p for p in _problems(lambda d: d["steps"][3]["do"].update(output="other")))
    assert any("not a state" in p for p in _problems(lambda d: d["success"].update(state="nowhere")))
    assert any("unknown input" in p for p in _problems(
        lambda d: d["steps"][2]["do"]["that"].update(input="ghost")))


def test_templates_must_reference_declared_inputs_and_tokens_must_be_lifted():
    assert any("undeclared input" in p for p in _problems(
        lambda d: d["steps"][0]["do"].update(route="/x?q=${nope}")))
    assert any("unlifted" in p for p in _problems(
        lambda d: d["steps"][0]["do"].update(route="/x?q={{customer_code}}")))
    assert any("undeclared input" in p for p in _problems(
        lambda d: d["targets"]["customer_link"]["strategies"][0].update(row_key="${nope}")))


def test_outputs_must_be_extracted_on_the_main_path():
    def drop_extract(d):
        d["steps"].pop()

    assert any("not extracted" in p for p in _problems(drop_extract))

    def extra_output(d):
        d["contract"]["outputs"]["other"] = {"type": "string"}

    assert any("every declared output" in p for p in _problems(extra_output))


def test_fingerprints_hold_labels_not_data():
    def amount(d):
        d["targets"]["outstanding"]["fingerprint"]["row_label"] = "Invoices 199.75 EUR Current outstanding bill"

    assert any("fingerprint" in p and "amount" in p for p in _problems(amount))

    def literal_code(d):
        d["targets"]["shown_code"]["fingerprint"]["row_label"] = "Customer Code CU1212-0005"

    assert any("fingerprint" in p and "literal" in p for p in _problems(literal_code))

    def email(d):
        d["targets"]["shown_code"]["fingerprint"]["row_label"] = "Mail a@b.example"

    assert any("fingerprint" in p and "personal data" in p for p in _problems(email))


def test_an_outcome_returns_only_outputs_extracted_before_its_branch():
    def early(d):
        d["contract"]["outcomes"] = {"found_early": {"description": "x", "returns": ["outstanding_amount"]}}
        d["steps"][1]["on"] = [{"state": "customer_list", "outcome": "found_early"}]

    assert any("before any step has extracted" in p for p in _problems(early))

    def late(d):
        d["contract"]["outcomes"] = {"found_late": {"description": "x", "returns": ["outstanding_amount"]}}
        d["steps"][3]["on"] = [{"state": "customer_card", "outcome": "found_late"}]

    assert not any("before any step" in p for p in _problems(late))


def test_volatile_text_is_amounts_emails_and_phones_only():
    from cua.artifact.validate import volatile_text

    assert all(volatile_text(t) for t in ("Invoices 199.75 €", "1,234.50", "a@b.example", "+33 1 23 45 67 89"))
    assert not any(volatile_text(t) for t in ("Customer Code", "Nature of Third party", "Q1 2026", "Invoices (30)"))
    # a whole number is an amount only next to its currency
    assert all(volatile_text(t) for t in ("Do you confirm this payment input for 50 Euros?", "€50", "50 EUR", "USD 50",
                                          "$ 1,200"))
    assert not any(volatile_text(t) for t in ("Bank account (EUR)", "Europe", "Currency", "Step 2 of 3"))


def test_effect_ceiling_must_match_the_steps():
    assert any("maximum step effect" in p for p in _problems(lambda d: d.update(effect="commit")))
    assert any("exceeds" in p for p in _problems(lambda d: d["steps"][0].update(effect="commit")))


def test_first_step_must_start_in_the_entry_state():
    assert any("entry state" in p for p in _problems(lambda d: d["steps"][0].update(expect="customer_card")))


def test_branches_must_end_in_declared_outcomes():
    def branch(d):
        d["steps"][1]["on"] = [{"state": "customer_list", "outcome": "customer_not_found"}]

    assert any("undeclared outcome" in p for p in _problems(branch))


def test_app_must_match_the_profile():
    prof = dolibarr_profile().model_copy(update={"app": "other"})
    assert any("does not match profile app" in p for p in validate_capability(capability(), prof))


def test_profile_problems():
    prof = dolibarr_profile()
    bad = prof.model_copy(update={"entry": prof.entry.model_copy(update={"start_state": "nowhere"})})
    assert any("start_state" in p for p in validate_profile(bad))
    states = dict(prof.states)
    states["customer_card"] = states["customer_card"].model_copy(update={"specializes": "customer_card"})
    assert any("cycle" in p for p in validate_profile(prof.model_copy(update={"states": states})))
    with pytest.raises(ArtifactInvalid) as e:
        require_valid(capability(), bad)
    assert e.value.problems
