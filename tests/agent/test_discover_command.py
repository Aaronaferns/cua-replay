"""`cua discover`: wiring, argument checks, and what the model is shown."""

from __future__ import annotations

import pytest

from cua.agent.commands import _specs
from cua.agent.model import render_observation, shorten_long_names
from cua.cli import build_parser
from cua.surface.base import Observation, PageFacts


def parse(*argv):
    return build_parser().parse_args(["discover", *argv])


def test_discover_is_wired_into_the_cli():
    args = parse("lookup_customer_outstanding", "--goal", "g {{customer_code}}", "--input", "customer_code=CU2601-00901")
    assert args.capability == "lookup_customer_outstanding" and args.effort == "high" and args.headed is False
    assert callable(args.func)
    assert parse("x", "--goal", "g", "--effort", "medium", "--headed").effort == "medium"


def test_known_capability_brings_its_specs_and_validates_inputs():
    args = parse("lookup_customer_outstanding", "--goal", "Find {{customer_code}}", "--input", "customer_code=CU2601-00901")
    values, inputs, outputs = _specs(args)
    assert values == {"customer_code": "CU2601-00901"}
    assert inputs["customer_code"].sensitivity == "pii" and outputs["outstanding_amount"].type == "decimal"
    bad = parse("lookup_customer_outstanding", "--goal", "Find {{customer_code}}", "--input", "customer_code=nope")
    with pytest.raises(SystemExit, match="pattern"):
        _specs(bad)


def test_goal_tokens_need_inputs_and_unknown_capabilities_need_outputs():
    with pytest.raises(SystemExit, match="customer_code"):
        _specs(parse("lookup_customer_outstanding", "--goal", "Find {{customer_code}}"))
    with pytest.raises(SystemExit, match="--output"):
        _specs(parse("something_else", "--goal", "g"))
    _, _, outputs = _specs(parse("something_else", "--goal", "g", "--output", "total:decimal"))
    assert outputs["total"].type == "decimal"


def test_long_accessible_names_are_shortened_for_the_model_only():
    help_text = "For searching inside text fields, you can use the characters ^ or $ " * 6
    snapshot = f'- textbox "{help_text}" [ref=e1]: typed\n- link "Short name" [ref=e2]'
    shown = shorten_long_names(snapshot)
    assert len(shown) < 200 and "[ref=e1]: typed" in shown and 'link "Short name" [ref=e2]' in shown
    obs = Observation(epoch=1, facts=PageFacts(url="http://x", title="T"), snapshot=snapshot)
    assert "..." in render_observation(obs) and obs.snapshot == snapshot  # the recorded snapshot is untouched
