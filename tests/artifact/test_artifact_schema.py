"""Schema strictness, backward-compatible seeded types, the scope firewall and the leaf-module rule."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import get_args

import pytest
from pydantic import BaseModel, ValidationError
from sample_docs import capability, capability_dict, dolibarr_profile

import cua.artifact as artifact_pkg
from cua.artifact.schema import (
    Capability,
    Effect,
    FieldSpec,
    Fingerprint,
    Profile,
    Strategy,
    max_effect,
    template_refs,
)

ARTIFACT_DIR = Path(artifact_pkg.__file__).parent
SECRET = "CU1910-00021"


def test_sample_documents_validate():
    assert capability().id == "dolibarr.lookup_customer_outstanding"
    assert dolibarr_profile().app == "dolibarr"


def test_unknown_fields_are_rejected_everywhere_and_never_echoed():
    doc = capability_dict()
    doc["steps"][0]["do"]["unexpected"] = SECRET
    with pytest.raises(ValidationError) as e:
        Capability.model_validate(doc)
    assert SECRET not in str(e.value) and "extra_forbidden" in str(e.value)

    doc = capability_dict()
    doc["contract"]["inputs"]["customer_code"]["pattern"] = SECRET + "("  # bad regex
    with pytest.raises(ValidationError) as e:
        Capability.model_validate(doc)
    assert SECRET not in str(e.value)


def test_every_model_is_strict_frozen_and_hides_input():
    def models(cls: type[BaseModel], seen: set[type]) -> None:
        if cls in seen:
            return
        seen.add(cls)
        for sub in cls.__subclasses__():
            models(sub, seen)

    seen: set[type] = set()
    models(BaseModel, seen)
    ours = [m for m in seen if m.__module__.startswith("cua.artifact.")]
    assert len(ours) > 20
    for m in ours:
        cfg = m.model_config
        assert cfg.get("extra") == "forbid", m
        assert cfg.get("hide_input_in_errors") is True, m


def test_discriminated_unions_are_closed():
    doc = capability_dict()
    doc["steps"][0]["do"] = {"kind": "script", "code": "alert(1)"}
    with pytest.raises(ValidationError):
        Capability.model_validate(doc)
    for kind in ("click_xy", "drag", "screenshot", "eval", "upload"):
        doc = capability_dict()
        doc["steps"][0]["do"] = {"kind": kind}
        with pytest.raises(ValidationError):
            Capability.model_validate(doc)


def test_effect_has_no_write():
    assert set(get_args(Effect)) == {"none", "read", "commit"}
    doc = capability_dict()
    doc["effect"] = "write"
    with pytest.raises(ValidationError):
        Capability.model_validate(doc)
    assert max_effect(["none", "commit", "read"]) == "commit" and max_effect([]) == "none"


def test_scope_firewall_no_design_only_fields():
    banned = {"family", "versions", "tenant", "overlay", "waiver", "waivers", "coverage", "read_back", "readback",
              "threshold", "policy", "run_handle", "runhandle", "pixel", "coordinates", "x", "y", "write"}
    found: set[str] = set()

    def keys(node) -> None:
        if isinstance(node, dict):
            found.update(k.lower() for k in node.get("properties", {}) if isinstance(k, str))
            for v in node.values():
                keys(v)
        elif isinstance(node, list):
            for v in node:
                keys(v)

    for model in (Capability, Profile):
        keys(model.model_json_schema())
    assert not (found & banned), found & banned
    assert "family" not in Profile.model_fields and "versions" not in Profile.model_fields
    assert "family" not in str(Capability.model_json_schema())


def test_seeded_shared_types_keep_their_shape():
    assert set(FieldSpec.model_fields) == {"type", "description", "pattern", "sensitivity"}
    assert set(Fingerprint.model_fields) == {"role", "tag", "input_type", "frame", "row_label", "column_header"}
    assert set(Strategy.model_fields) == {
        "kind", "role", "name", "label", "take", "row_key", "column", "within_role", "within_name", "css",
        "why", "matched_at_record",
    }
    FieldSpec(type="decimal")
    Fingerprint(role="textbox", tag="input")


def test_strategy_fields_must_fit_the_kind():
    ok = dict(why="w", matched_at_record="1 of 1")
    Strategy(kind="role", role="textbox", name="Login", **ok)
    Strategy(kind="same_row", label="Customer Code", take="value", **ok)
    Strategy(kind="table_cell", column="Status", row_key="x", **ok)
    Strategy(kind="scoped", role="textbox", within_role="columnheader", within_name="Customer Code", **ok)
    Strategy(kind="css", css="button[name='x']", **ok)
    for bad in (
        dict(kind="role", role="textbox"),  # name missing
        dict(kind="role", role="textbox", name="a", css="x"),  # a field the kind does not use
        dict(kind="same_row", label="Customer Code"),  # take missing
        dict(kind="scoped", role="textbox", within_role="columnheader"),
        dict(kind="css"),
    ):
        with pytest.raises(ValidationError):
            Strategy(**bad, **ok)


def test_strategy_helpers_never_expose_why():
    s = Strategy(kind="role", role="link", name="${customer_code}", why="secret reason", matched_at_record="x")
    assert s.locator_strings() == ["link", "${customer_code}"]
    assert "secret reason" not in s.describe()
    assert template_refs("a ${x} b ${y_2} ${Bad}") == ["x", "y_2"]


def test_navigate_route_must_be_relative():
    for bad in ("https://evil.example/", "//evil.example/x", "societe/list.php"):
        doc = capability_dict()
        doc["steps"][0]["do"]["route"] = bad
        with pytest.raises(ValidationError):
            Capability.model_validate(doc)


def test_ids_and_versions_are_checked():
    for field, bad in (("id", "Lookup"), ("id", "dolibarr"), ("version", "1.0"), ("app", "Dolibarr")):
        doc = capability_dict()
        doc[field] = bad
        with pytest.raises(ValidationError):
            Capability.model_validate(doc)
    doc = capability_dict()
    doc["steps"][0]["id"] = "Step One"
    with pytest.raises(ValidationError):
        Capability.model_validate(doc)


def test_target_ids_are_names():
    doc = capability_dict()
    doc["targets"]["Bad Id"] = doc["targets"]["outstanding"]
    with pytest.raises(ValidationError):
        Capability.model_validate(doc)


def test_a_branch_needs_exactly_one_destination():
    doc = capability_dict()
    doc["steps"][0]["on"] = [{"state": "customer_list"}]
    with pytest.raises(ValidationError):
        Capability.model_validate(doc)
    doc["steps"][0]["on"] = [{"state": "customer_list", "then": "next", "outcome": "x"}]
    with pytest.raises(ValidationError):
        Capability.model_validate(doc)


def test_the_hashed_body_has_no_status_approval_or_hash():
    banned = {"status", "approval", "approved", "approver", "lock_hash", "hash", "sha256"}
    fields = set(Capability.model_fields) | set(Capability.model_fields["provenance"].annotation.model_fields)
    assert not (fields & banned)
    assert set(Capability.model_fields["provenance"].annotation.model_fields) == {
        "trace_run_id", "trace_sha256", "discovered_by", "compiler"}


def test_documents_round_trip_by_alias():
    cap = capability()
    data = json.loads(cap.model_dump_json(by_alias=True, exclude_none=True))
    assert data["schema"] == "cua.capability/v1"
    assert Capability.model_validate(data) == cap


# --- the artifact is a leaf with no browser-engine vocabulary ---------------------------------------------------

FORBIDDEN_WORDS = (
    "playwright", "get_by_", "getbyrole", "aria-ref", "aria_ref", "frame_locator", "page.locator", "xpath",
    "mouse.", "pixel", "coordinate", "screenshot", "selenium", "cdp",
)


def test_artifact_has_no_playwright_concepts():
    files = sorted(ARTIFACT_DIR.glob("*.py"))
    assert len(files) >= 9
    for path in files:
        text = path.read_text(encoding="utf-8").lower()
        for word in FORBIDDEN_WORDS:
            assert word not in text, f"{path.name} mentions {word!r}"
    # the documents themselves carry none either
    blob = json.dumps(Capability.model_json_schema()).lower() + json.dumps(Profile.model_json_schema()).lower()
    for word in FORBIDDEN_WORDS:
        assert word not in blob


def test_artifact_imports_only_pydantic_and_the_standard_library():
    allowed_external = {"pydantic"}
    stdlib = set(sys.stdlib_module_names)
    for path in sorted(ARTIFACT_DIR.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module]
            for mod in mods:
                top = mod.split(".")[0]
                if top == "cua":
                    assert mod.startswith("cua.artifact"), f"{path.name} imports {mod}"
                else:
                    assert top in stdlib | allowed_external | {"__future__"}, f"{path.name} imports {mod}"
