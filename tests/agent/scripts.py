"""A scripted (non-model) driver for `lookup_customer_outstanding` on the live Dolibarr target: the same moves a model
makes, with refs picked by role and name. Shared by the live discovery test and the S3 harness."""

from __future__ import annotations

from cua.agent.actions import Click, Done, Extract, Navigate, Press, Type
from cua.artifact.schema import FieldSpec
from cua.surface import snapshot as snap
from cua.surface.base import Observation
from cua.surface.names import same_name

GOAL = "Find the current outstanding bill amount of the customer with customer code {{customer_code}}."
INPUT_SPECS = {"customer_code": FieldSpec(type="string", pattern=r"CU\d{4}-\d{4,5}", sensitivity="pii",
                                          description="the customer code")}
OUTPUT_SPECS = {"outstanding_amount": FieldSpec(type="decimal", description="the current outstanding bill amount")}


def ref_of(obs: Observation, role: str, name: str, *, contains: bool = False) -> str:
    for node in snap.parse(obs.dialog_snapshot or obs.snapshot):
        if node.role != role or not node.ref or node.name is None:
            continue
        if (name.casefold() in node.name.casefold()) if contains else same_name(name, node.name):
            return node.ref
    raise AssertionError(f"no {role} {name!r} on the page")


def code_filter_ref(obs: Observation) -> str:
    """Dolibarr's list filters share one long accessible name; the fourth is the customer code."""
    boxes = [n for n in snap.parse(obs.snapshot) if n.role == "textbox" and n.name and n.name.startswith("For searching")]
    return boxes[3].ref  # type: ignore[return-value]


def lookup_script(base_url: str) -> list:
    return [
        Navigate(rationale="Open the customer list", url=f"{base_url}/societe/list.php?leftmenu=customers&type=c"),
        lambda obs: Type(rationale="Filter by customer code {{customer_code}}", ref=code_filter_ref(obs),
                         text="{{customer_code}}"),
        Press(rationale="Apply the filter", key="Enter"),
        lambda obs: Click(rationale="Open the only matching customer",
                          ref=_first_customer_link(obs)),
        lambda obs: Click(rationale="Open the Customer tab", ref=ref_of(obs, "link", "Customer")),
        lambda obs: Extract(rationale="Read the current outstanding bill", name="outstanding_amount",
                            ref=ref_of(obs, "generic", "Current outstanding bill")),
        lambda obs: Done(rationale="The amount is extracted",
                         evidence_refs=[ref_of(obs, "generic", "Current outstanding bill")]),
    ]


def _first_customer_link(obs: Observation) -> str:
    """The name link of the first result row (links whose url opens a customer card)."""
    nodes = snap.parse(obs.snapshot)
    for i, node in enumerate(nodes):
        if node.role == "link" and node.ref and any(
                c.role == "/url" and (c.value or "").startswith("/societe/card.php?socid=") for c in nodes[i + 1 : i + 3]):
            return node.ref
    raise AssertionError("no customer link in the list")
