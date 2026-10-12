"""The review card is deterministic and shows what approval vouches for."""

from __future__ import annotations

import subprocess
import sys

from sample_docs import capability, capability_dict, dolibarr_profile

from cua.artifact.lock import lock_hash
from cua.artifact.schema import Capability
from cua.compiler.review_card import render_review_card


def card(cap=None, profile=None) -> str:
    return render_review_card(cap or capability(), profile or dolibarr_profile())


def test_the_card_is_deterministic_lf_text():
    first = card()
    assert first == card() == render_review_card(Capability.model_validate(capability_dict()), dolibarr_profile())
    assert "\r" not in first and first.endswith("\n") and not first.endswith("\n\n")


def test_the_card_names_the_lock_hash_and_changes_with_it():
    cap, prof = capability(), dolibarr_profile()
    assert f"`{lock_hash(cap, prof)}`" in card(cap, prof)
    edited = prof.model_copy(update={"default_meaning": {}})
    assert lock_hash(cap, edited) not in card(cap, prof)
    assert lock_hash(cap, edited) in card(cap, edited)


def test_the_card_shows_contract_steps_targets_success_and_states():
    text = card()
    for section in ("## Contract", "### Inputs", "### Outputs", "## Steps", "## Targets", "## Success checkpoint",
                    "## States used"):
        assert section in text
    # contract
    assert "`customer_code`" in text and "`outstanding_amount`" in text and "pii" in text
    # every step with its intent, expected state and effect
    for step in capability().steps:
        assert step.id in text and step.intent in text and f"starts in state `{step.expect}`" in text
    assert "effect **read**" in text and "effect **none**" in text
    assert "Commit steps: none" in text
    # the identity assert and the value echo are visible to the reviewer
    assert "equals input `customer_code`" in text
    # targets: primary and fallback strategies, with the fingerprint gate
    assert "- primary:" in text and "- fallback 1:" in text and "fingerprint" in text
    # success checkpoint and states, with the specialization and the default meaning
    assert "ends in state `customer_card`" in text and "check: target `shown_code` is found exactly once" in text
    assert "`customer_card`" in text and "origin " in text


def test_states_used_follow_the_steps_and_their_ancestors():
    doc = capability_dict()
    doc["contract"]["outcomes"] = {"customer_not_found": {"description": "No such customer."}}
    doc["steps"][0]["on"] = [{"state": "customer_not_found", "outcome": "customer_not_found"},
                             {"state": "customer_list", "then": "next"}]
    text = card(Capability.model_validate(doc))
    assert "`customer_not_found`:" in text and "specializes `customer_list`" in text
    assert "on state `customer_not_found`: ends with the business outcome `customer_not_found`" in text
    assert "default meaning: outcome `customer_not_found`" in text
    states = [line for line in text.splitlines() if line.startswith("- `") and "recognised" not in line and "origin" in line]
    assert len(states) == len(set(states))


def test_the_card_never_contains_an_input_value():
    assert "CU1212" not in card() and "CU1910" not in card()


def test_the_commit_routes_are_listed_for_a_commit_capability():
    doc = capability_dict()
    doc["effect"] = "commit"
    doc["steps"][3]["effect"] = "commit"
    text = card(Capability.model_validate(doc))
    assert "Commit steps: `s04_read_amount`" in text
    assert "POST /compta/paiement.php body action=confirm_paiement body confirm=yes" in text
    assert "GET * query action=confirm_*" in text


def test_the_card_module_loads_no_browser_engine_or_model_sdk():
    code = ("import sys, cua.compiler.review_card\n"
            "bad = [m for m in sys.modules if m.split('.')[0] in ('playwright', 'anthropic', 'openai', 'cua.replay', "
            "'cua.surface', 'cua.agent')]\nprint(bad)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"
