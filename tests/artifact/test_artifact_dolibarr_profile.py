"""apps/dolibarr/profile.json: loads, validates, and classifies the screens it describes (by content)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from sample_docs import PROFILE_PATH

from cua.artifact.classify import classify
from cua.artifact.lock import dumps, load_profile
from cua.artifact.predicates import ScreenView
from cua.artifact.schema import Profile
from cua.artifact.screen import parse_outline, view_from_outline
from cua.artifact.validate import validate_profile

LOGIN_FORM = '- textbox "Login" [active] [ref=e15]\n- textbox "Password" [ref=e19]\n- button "Login" [ref=e24]\n'


@pytest.fixture(scope="module")
def profile() -> Profile:
    return load_profile(PROFILE_PATH)


def test_profile_loads_and_validates(profile):
    assert profile.app == "dolibarr"
    assert validate_profile(profile) == []
    assert set(profile.states) == {
        "login", "home", "customer_list", "customer_not_found", "customer_card", "customer_tab", "invoice_card",
        "payment_form", "payment_confirm", "access_denied", "invoice_validate_confirm",
    }
    assert profile.entry.start_state in profile.states and profile.login.state in profile.states


def test_profile_file_is_in_normal_form(profile):
    assert PROFILE_PATH.read_text(encoding="utf-8") == dumps(profile)
    assert b"\r" not in PROFILE_PATH.read_bytes()


def test_login_form_uses_the_real_dolibarr_labels(profile):
    names = {k: getattr(profile.login, k).strategies[0].name for k in ("username", "password", "submit")}
    assert names == {"username": "Login", "password": "Password", "submit": "Login"}
    assert profile.login.password.fingerprint.input_type == "password"


def test_login_form_at_the_same_url_as_app_pages_is_classified_by_content(profile):
    for url in ("http://localhost:8086/index.php?mainmenu=home&leftmenu=home",
                "http://localhost:8086/societe/list.php?type=c", "http://localhost:8086/societe/card.php?socid=10"):
        assert classify(profile, view_from_outline(url, LOGIN_FORM)).label == "login"
    assert profile.default_meaning["login"].failure == "SESSION_EXPIRED"


def test_each_state_is_recognised(profile):
    screens = {
        "home": ("http://h/index.php?mainmenu=home&leftmenu=home", '- generic [ref=e1]: Global view\n'),
        "customer_list": ("http://h/societe/list.php?leftmenu=customers&type=c",
                          '- columnheader " Third-party name" [ref=e1]\n'),
        "customer_card": ("http://h/societe/card.php?socid=10",
                          '- cell "Nature of Third party"\n- cell "Customer Code" [ref=e2]\n'),
    }
    for state, (url, outline) in screens.items():
        assert classify(profile, view_from_outline(url, outline)).label == state


def test_content_without_the_right_url_is_not_a_state(profile):
    c = classify(profile, view_from_outline("http://h/compta/facture/card.php?id=3", '- cell "Customer Code"\n'))
    assert c.kind == "unknown"


OUTLINES = json.loads((Path(__file__).parent / "fixtures" / "dolibarr" / "outlines.json").read_text(encoding="utf-8"))


def _classify_stored(profile: Profile, entry: dict):
    page = view_from_outline(entry["url"], entry["outline"])
    dialog = None
    if "dialog" in entry:
        dialog = ScreenView(url=entry["url"], nodes=parse_outline(entry["dialog"]))
    return classify(profile, page, dialog)


@pytest.mark.parametrize("name", sorted(OUTLINES))
def test_classifier_on_stored_snapshots(profile, name):
    """Redacted outlines of the real Dolibarr 23.0.4 screens (S4, observed live): runs offline in a clean clone."""
    entry = OUTLINES[name]
    assert _classify_stored(profile, entry).label == entry["expected"]


def test_stored_outlines_hold_no_personal_data():
    blob = json.dumps(OUTLINES)
    assert "@" not in blob and not re.search(r"\+?\d[\d .-]{8,}\d", blob.replace("CU2601-00901", ""))


def test_every_state_in_the_profile_is_seen_in_a_stored_outline(profile):
    assert {e["expected"] for e in OUTLINES.values()} == set(profile.states)


def test_customer_not_found_specializes_customer_list(profile):
    assert profile.states["customer_not_found"].specializes == "customer_list"
    assert profile.is_a("customer_not_found", "customer_list")
    assert profile.default_meaning["customer_not_found"].outcome == "customer_not_found"


def test_the_customer_tab_is_not_the_customer_card(profile):
    outline = '- cell "Customer Code"'
    card = view_from_outline("http://h/societe/card.php?socid=1", outline)
    tab = view_from_outline("http://h/comm/card.php?socid=1", outline)
    assert classify(profile, card).label == "customer_card" and classify(profile, tab).label == "customer_tab"
