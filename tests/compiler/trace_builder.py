"""A hand-built RecordedTrace for lookup_customer_outstanding (what a discovery run would persist, redacted)."""

from __future__ import annotations

from datetime import UTC, datetime

from cua.agent.actions import Click, Done, Extract, Navigate, Press, Type
from cua.artifact.schema import FieldSpec, Fingerprint, Strategy
from cua.compiler.trace import (
    LocatorCandidate,
    RecordedAction,
    RecordedTrace,
    ScreenRecord,
    TargetRecord,
    TokenSighting,
)
from cua.safety.policy import PolicyDecision

BASE = "http://localhost:8086"
T0 = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
ALLOW = PolicyDecision(allowed=True, rule="default", reason="ok")

HOME = ("/index.php?mainmenu=home&leftmenu=home", "Home", "- generic [ref=e1]: Global view\n")
LIST = ("/societe/list.php?leftmenu=customers&type=c", "Customers",
        '- columnheader "Third-party name" [ref=e2]\n- textbox "Filter" [ref=e5]\n')
LIST_FILTERED = ("/societe/list.php?leftmenu=customers&type=c&search_code=*", "Customers",
                 '- columnheader "Third-party name" [ref=e2]\n- link "Acme" [ref=e9]\n')
CARD = ("/societe/card.php?socid=*", "Third party",
        '- cell "Nature of Third party" [ref=e1]\n- cell "Customer Code" [ref=e2]\n- generic [ref=e3]: ***\n')


def screen(page: tuple[str, str, str], epoch: int) -> ScreenRecord:
    url, title, snapshot = page
    return ScreenRecord(epoch=epoch, url=BASE + url, title=title, snapshot=snapshot)


def _strategy(kind: str = "role", **kw) -> Strategy:
    return Strategy(kind=kind, why="recorded", matched_at_record="1 of 1, same element as ref", **kw)


def target(ref: str, role: str, name: str, fingerprint: Fingerprint, *strategies: tuple[int, Strategy]) -> TargetRecord:
    return TargetRecord(ref=ref, role=role, name=name, fingerprint=fingerprint,
                        candidates=[LocatorCandidate(rank=r, strategy=s, match_count=1, same_element=True)
                                    for r, s in strategies])


FILTER = target("e5", "textbox", "Filter", Fingerprint(role="textbox", tag="input", input_type="text",
                                                      column_header="Customer Code"),
                (4, _strategy("scoped", role="textbox", within_role="columnheader", within_name="Customer Code")),
                (6, _strategy("css", css="input[name='search_code']")))
ROW_LINK = target("e9", "link", "Acme", Fingerprint(role="link", tag="a", column_header="Third-party name"),
                  (6, _strategy("css", css="a.classfortooltip")),
                  (3, _strategy("table_cell", role="link", column="Third-party name", row_key="{{customer_code}}")))
CODE_CELL = target("e3", "generic", "***", Fingerprint(role="cell", tag="td", row_label="Customer Code"),
                   (2, _strategy("same_row", label="Customer Code", take="value")))
AMOUNT = target("e7", "link", "Current outstanding bill", Fingerprint(role="link", tag="a",
                                                                       row_label="Current outstanding bill"),
                (2, _strategy("same_row", label="Current outstanding bill", take="value")),
                (1, _strategy("role", role="link", name="Current outstanding bill")))


def recorded(seq: int, action, pre, post=None, *, effect="read", status="performed", target_rec=None, tokens=(),
             sightings=(), error=None) -> RecordedAction:
    return RecordedAction(seq=seq, action=action, tokens=list(tokens), effect=effect, policy=ALLOW, status=status,
                          error=error, target=target_rec, pre=pre, post=post, sightings=list(sightings),
                          started_at=T0, duration_ms=120)


def build_trace(**overrides) -> RecordedTrace:
    s_home, s_list, s_filtered, s_card = (screen(HOME, 1), screen(LIST, 2), screen(LIST_FILTERED, 3), screen(CARD, 4))
    actions = [
        recorded(1, Navigate(rationale="Open the customer list", url=BASE + "/societe/list.php?leftmenu=customers&type=c"),
                 s_home, s_list),
        recorded(2, Type(rationale="Filter by customer code {{customer_code}}", ref="e5", text="{{customer_code}}"),
                 s_list, s_list, effect="none", target_rec=FILTER, tokens=["customer_code"]),
        recorded(3, Press(rationale="Submit the filter", key="Enter"), s_list, s_filtered),
        recorded(4, Click(rationale="Open the only matching customer", ref="e9"), s_filtered, s_card,
                 target_rec=ROW_LINK,
                 sightings=[TokenSighting(token="customer_code", target=CODE_CELL)]),
        recorded(5, Extract(rationale="Read the current outstanding bill", name="outstanding_amount", ref="e7"),
                 s_card, s_card, target_rec=AMOUNT),
        recorded(6, Done(rationale="The amount is shown", evidence_refs=["e3"]), s_card, None, effect="none"),
    ]
    data = dict(
        run_id="run-0001", app="dolibarr", goal="Find the outstanding amount for customer {{customer_code}}",
        model="scripted", started_at=T0,
        inputs={"customer_code": FieldSpec(type="string", pattern=r"CU\d{4}-\d{4,5}", sensitivity="pii")},
        outputs={"outstanding_amount": FieldSpec(type="decimal")},
        entry_url=BASE + "/index.php", actions=actions, outcome="done", done_evidence=[CODE_CELL],
    )
    data.update(overrides)
    return RecordedTrace(**data)

