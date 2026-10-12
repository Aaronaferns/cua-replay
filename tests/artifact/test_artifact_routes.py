"""Commit routes: the profile says which requests commit; the matcher is pure."""

from __future__ import annotations

import pytest
from sample_docs import dolibarr_profile

from cua.artifact.routes import is_commit, route_matches
from cua.artifact.schema import RoutePattern

BASE = "http://localhost:8086"
PAY = {"action": "confirm_paiement", "confirm": "yes", "token": "x", "facid": "7"}


@pytest.fixture(scope="module")
def profile():
    return dolibarr_profile()


@pytest.mark.parametrize("url", [
    f"{BASE}/compta/facture/card.php?facid=12&action=confirm_valid&confirm=yes&token=abc",
    "/compta/facture/card.php?action=confirm_valid&confirm=yes",
    f"{BASE}/compta/facture/card.php?id=3&action=confirm_delete&confirm=yes&token=abc",
    f"{BASE}/fourn/facture/card.php?action=confirm_validate",  # any confirm_* GET raises the class
])
def test_invoice_confirmations_are_commits(profile, url):
    assert is_commit(profile, "GET", url)


@pytest.mark.parametrize("url", [
    f"{BASE}/compta/facture/card.php?facid=12&action=valid",  # opens the confirmation dialog, commits nothing
    f"{BASE}/compta/facture/card.php?facid=12",
    f"{BASE}/societe/list.php?type=c&search_code=CU1212-0005",
    f"{BASE}/comm/card.php?socid=10",
])
def test_reads_and_dialog_openers_are_not_commits(profile, url):
    assert not is_commit(profile, "GET", url)


def test_the_payment_is_a_post_identified_by_its_body(profile):
    url = f"{BASE}/compta/paiement.php"
    assert is_commit(profile, "POST", url, PAY)
    assert is_commit(profile, "post", url, PAY)  # method case does not matter
    # the first POST (action=add_paiement) only asks for confirmation
    assert not is_commit(profile, "POST", url, {"action": "add_paiement", "token": "x"})
    assert not is_commit(profile, "POST", url, {**PAY, "confirm": "no"})
    # the route, not the body alone
    assert not is_commit(profile, "POST", f"{BASE}/compta/other.php", PAY)


def test_unknown_body_counts_as_a_possible_commit(profile):
    assert is_commit(profile, "POST", f"{BASE}/compta/paiement.php", None)
    assert not is_commit(profile, "POST", f"{BASE}/societe/list.php", None)


def test_method_any_matches_every_method_and_a_specific_method_only_its_own():
    anymethod = RoutePattern(path="/x", query={"go": "yes"})
    assert route_matches(anymethod, "GET", "/x?go=yes") and route_matches(anymethod, "POST", "/x?go=yes")
    get_only = RoutePattern(method="GET", path="/x")
    assert route_matches(get_only, "GET", "/x") and not route_matches(get_only, "POST", "/x")


def test_globs_are_anchored_and_case_sensitive():
    p = RoutePattern(path="/a/*.php", query={"action": "confirm_*"})
    assert route_matches(p, "GET", "/a/card.php?action=confirm_valid")
    assert not route_matches(p, "GET", "/a/card.php?action=Confirm_valid")
    assert not route_matches(p, "GET", "/a/card.php?action=preconfirm_valid")
    assert not route_matches(p, "GET", "/a/card.phpx?action=confirm_valid")


def test_a_repeated_query_key_matches_if_any_value_does():
    p = RoutePattern(path="*", query={"action": "confirm_valid"})
    assert route_matches(p, "GET", "/x?action=view&action=confirm_valid")


def test_the_route_schema_is_strict():
    with pytest.raises(ValueError):
        RoutePattern(path="/x", method="PUT")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        RoutePattern(path="/x", regex=".*")  # type: ignore[call-arg]
