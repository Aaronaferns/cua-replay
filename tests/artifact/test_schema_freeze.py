"""The schema is frozen at M3 exit: the JSON Schema of `Capability` and `Profile` is pinned to a stored copy.

Any change to either model changes `model_json_schema()` and fails this test. That is the point: a change to the
artifact language is a versioned decision (a new `schema` string, a migration, a re-approval), never a drive-by edit.
To make such a change on purpose, bump the schema version in the model, regenerate the fixture with

    uv run python tests/artifact/test_schema_freeze.py --write

and review the diff of `fixtures/schema_v1.json` (or add `schema_v2.json` and retarget this test).

The scope-firewall test below holds the schema to `docs/contracts.md`: no design-only item may appear in it.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from cua.artifact.schema import Capability, Profile

FIXTURE = Path(__file__).parent / "fixtures" / "schema_v1.json"
CONTRACTS = Path(__file__).resolve().parents[2] / "docs" / "contracts.md"


def current_schema() -> dict:
    return {"capability": Capability.model_json_schema(), "profile": Profile.model_json_schema()}


def stored_schema() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_schema_matches_the_frozen_copy():
    assert current_schema() == stored_schema()


def test_the_frozen_copy_is_the_v1_documents():
    stored = stored_schema()
    assert stored["capability"]["properties"]["schema"]["const"] == "cua.capability/v1"
    assert stored["profile"]["properties"]["schema"]["const"] == "cua.profile/v1"


def _keys_and_enums(node, out: set[str]) -> None:
    if isinstance(node, dict):
        out.update(k.lower() for k in node.get("properties", {}))
        for value in node.get("enum", []):
            if isinstance(value, str):
                out.add(value.lower())
        for value in node.values():
            _keys_and_enums(value, out)
    elif isinstance(node, list):
        for item in node:
            _keys_and_enums(item, out)


# Names of the design-only items in the contracts' scope firewall that could plausibly leak into a schema field,
# an enum member or a union tag. Each is a word a model author would reach for.
DESIGN_ONLY = {
    "write", "family", "versions", "runhandle", "daemon", "desktop", "pixel", "coordinate", "coordinates",
    "threshold", "hierarchy", "waiver", "waivers", "quarantine", "canary", "canaries", "ocr", "selenium",
    "webarena", "overlay", "tenant",
}


def test_scope_firewall_no_design_only_item_in_the_schema():
    names: set[str] = set()
    _keys_and_enums(current_schema(), names)
    assert not (names & DESIGN_ONLY), sorted(names & DESIGN_ONLY)


def test_scope_firewall_list_in_the_contracts_is_what_this_test_guards():
    if not CONTRACTS.is_file():  # a clean clone of a tag may not ship docs
        return
    text = re.sub(r"\s+", " ", CONTRACTS.read_text(encoding="utf-8")).lower()
    for item in ("effect `write`", "family/version binding layers", "runhandle/daemon",
                 "desktop, pixel or coordinate actions", "policy hierarchy or threshold expressions",
                 "quarantine/canaries", "waivers"):
        assert item in text, item


if __name__ == "__main__":
    if "--write" in sys.argv:
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(json.dumps(current_schema(), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                           encoding="utf-8", newline="\n")
        print(f"wrote {FIXTURE}")
