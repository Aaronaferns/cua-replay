"""Hand-built sample documents shared by the artifact, compiler and replay tests (importable via conftest paths)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cua.artifact.lock import load_profile
from cua.artifact.schema import Capability, Profile

REPO = Path(__file__).resolve().parents[2]
PROFILE_PATH = REPO / "apps" / "dolibarr" / "profile.json"

LIST_ROUTE = "/societe/list.php?leftmenu=customers&type=c&search_code=${customer_code}"


def dolibarr_profile() -> Profile:
    return load_profile(PROFILE_PATH)


def _role(role: str, name: str) -> dict[str, Any]:
    return {"kind": "role", "role": role, "name": name, "why": "test", "matched_at_record": "1 of 1"}


def capability_dict() -> dict[str, Any]:
    """lookup_customer_outstanding: list filtered by code, open the card, check identity, read the amount."""
    return {
        "schema": "cua.capability/v1",
        "id": "dolibarr.lookup_customer_outstanding",
        "version": "1.0.0",
        "title": "Look up a customer's outstanding amount",
        "description": "Open the customer's card by code and read the current outstanding bill.",
        "app": "dolibarr",
        "contract": {
            "inputs": {"customer_code": {"type": "string", "pattern": "CU[0-9]{4}-[0-9]{4,5}", "sensitivity": "pii"}},
            "outputs": {"outstanding_amount": {"type": "decimal"}},
            "outcomes": {},
        },
        "effect": "read",
        "steps": [
            {"id": "s01_open_list", "intent": "Open the customer list filtered by the code", "expect": "home",
             "do": {"kind": "navigate", "route": LIST_ROUTE}, "effect": "read", "navigates": True},
            {"id": "s02_open_card", "intent": "Open the customer's card", "expect": "customer_list",
             "do": {"kind": "click", "target": "customer_link"}, "effect": "read", "navigates": True},
            {"id": "s03_identity", "intent": "The card shows the requested customer code", "expect": "customer_card",
             "do": {"kind": "assert", "that": {"kind": "target_equals_input", "target": "shown_code",
                                               "input": "customer_code", "normalizer": "text"},
                    "on_fail": "IDENTITY_MISMATCH"},
             "effect": "none"},
            {"id": "s04_read_amount", "intent": "Read the current outstanding bill", "expect": "customer_card",
             "do": {"kind": "extract", "target": "outstanding", "output": "outstanding_amount"}, "effect": "read"},
        ],
        "success": {
            "state": "customer_card",
            "outputs": ["outstanding_amount"],
            "checks": [{"kind": "target_visible", "target": "shown_code"}],
        },
        "targets": {
            "customer_link": {
                "description": "the customer's name link in the results row",
                "strategies": [
                    {"kind": "table_cell", "role": "link", "column": "Third-party name", "row_key": "${customer_code}",
                     "why": "link in the name column of the row keyed by the code", "matched_at_record": "1 of 1"},
                    {"kind": "same_row", "label": "Customer Code", "take": "control", "role": "link",
                     "why": "fallback", "matched_at_record": "1 of 1"},
                ],
                "fingerprint": {"role": "link", "tag": "a", "column_header": "Third-party name"},
            },
            "shown_code": {
                "description": "the value cell of the Customer Code row",
                "strategies": [{"kind": "same_row", "label": "Customer Code", "take": "value",
                                "why": "value cell of the labelled row", "matched_at_record": "1 of 1"}],
                "fingerprint": {"role": "cell", "tag": "td", "row_label": "Customer Code"},
            },
            "outstanding": {
                "description": "the current outstanding bill amount",
                "strategies": [
                    {"kind": "same_row", "label": "Current outstanding bill", "take": "value",
                     "why": "value beside the label", "matched_at_record": "1 of 1"},
                    _role("link", "Current outstanding bill"),
                ],
                "fingerprint": {"role": "link", "tag": "a", "row_label": "Current outstanding bill"},
            },
        },
        "provenance": {"trace_run_id": "run-test", "trace_sha256": "0" * 64, "discovered_by": "alice",
                       "compiler": "cua.compiler/v0"},
    }


def capability() -> Capability:
    return Capability.model_validate(capability_dict())
