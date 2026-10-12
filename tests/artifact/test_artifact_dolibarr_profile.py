"""apps/dolibarr/profile.json: loads, validates, and classifies the screens it describes (by content)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sample_docs import PROFILE_PATH, REPO

from cua.artifact.classify import classify
from cua.artifact.lock import dumps, load_profile
from cua.artifact.schema import Profile
from cua.artifact.screen import view_from_outline
from cua.artifact.validate import validate_profile

LOGIN_FORM = '- textbox "Login" [active] [ref=e15]\n- textbox "Password" [ref=e19]\n- button "Login" [ref=e24]\n'


@pytest.fixture(scope="module")
def profile() -> Profile:
    return load_profile(PROFILE_PATH)


def test_profile_loads_and_validates(profile):
    assert profile.app == "dolibarr"
    assert validate_profile(profile) == []
    assert set(profile.states) == {"login", "home", "customer_list", "customer_card"}
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


SPIKE_OUT = Path(os.environ.get("CUA_SPIKE_OUT") or REPO / "spikes" / "dolibarr" / "out")
STORED = {
    "01_login": ("http://localhost:8086/index.php?mainmenu=home&leftmenu=home", "login"),
    "02_home": ("http://localhost:8086/index.php?mainmenu=home&leftmenu=home", "home"),
    "03_customer_list": ("http://localhost:8086/societe/list.php?leftmenu=customers&type=c", "customer_list"),
    "04_customer_card": ("http://localhost:8086/societe/card.php?socid=10", "customer_card"),
    "12_search_nearmiss": ("http://localhost:8086/societe/list.php?search_code=CU1910-0002", "customer_list"),
    "05_draft_invoice_list": ("http://localhost:8086/compta/facture/list.php?search_status=0", "unknown"),
    "10_noperm_invoice": ("http://localhost:8086/compta/facture/card.php?id=1", "unknown"),
}


@pytest.mark.skipif(not (SPIKE_OUT / "01_login.snapshot.txt").exists(),
                    reason="captured Dolibarr snapshots are local-only (spikes/dolibarr/out, or set CUA_SPIKE_OUT)")
@pytest.mark.parametrize("name", sorted(STORED))
def test_classifier_on_stored_snapshots(profile, name):
    url, expected = STORED[name]
    text = (SPIKE_OUT / f"{name}.snapshot.txt").read_text(encoding="utf-8")
    assert classify(profile, view_from_outline(url, text)).label == expected
