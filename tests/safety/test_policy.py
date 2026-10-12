"""Policy file loading and the PolicyGuard: unknown means deny, and reasons never carry input values."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cua.control.lease import AUTOMATION, ControlLease, LeaseToken, human
from cua.safety.policy import (
    ActionRequest,
    Policy,
    PolicyDenied,
    PolicyError,
    PolicyGuard,
    load_policy,
)

APP = "http://localhost:8087"


def make_policy(**overrides) -> Policy:
    data = {
        "version": "cua.policy/v1",
        "name": "t",
        "sandbox": True,
        "origins": [APP],
        "routes": {"allow": ["/", "/index.php", "/societe/**"], "deny": ["/societe/admin/**"]},
        "actions": ["click", "type", "navigate", "extract"],
        "max_effect": "read",
    }
    data.update(overrides)
    return Policy.model_validate(data)


class Lease:
    """A mutable lease the guard reads on every check."""

    def __init__(self, holder: str = AUTOMATION, generation: int = 1):
        self.holder, self.generation = holder, generation

    def __call__(self) -> LeaseToken:
        return LeaseToken(holder=self.holder, generation=self.generation)


def request(**overrides) -> ActionRequest:
    data = {
        "kind": "click",
        "actor": "replay",
        "page_url": f"{APP}/societe/card.php?socid=1",
        "effect": "none",
        "lease": LeaseToken(holder=AUTOMATION, generation=1),
    }
    data.update(overrides)
    return ActionRequest(**data)


@pytest.fixture
def lease() -> Lease:
    return Lease()


@pytest.fixture
def guard(lease: Lease) -> PolicyGuard:
    return PolicyGuard(make_policy(), lease)


# ----------------------------------------------------------------------------------------------- the policy file


def test_dolibarr_sandbox_policy_resolves_the_port_from_variables():
    policy = load_policy("dolibarr-sandbox", variables={"DOLIBARR_PORT": "8087"})
    assert policy.sandbox is True
    assert policy.origins == ["http://localhost:8087", "http://127.0.0.1:8087"]
    assert policy.check_url("http://127.0.0.1:8087/societe/card.php?socid=1").allowed
    assert not policy.check_url("http://localhost:8085/index.php").allowed  # another agent's target


def test_dolibarr_sandbox_policy_defaults_to_the_configured_port():
    from cua.config import settings

    policy = load_policy("dolibarr-sandbox")
    assert f"http://localhost:{settings().dolibarr_port}" in policy.origins


def test_unknown_placeholder_is_an_error_not_a_literal(tmp_path):
    path = tmp_path / "p.json"
    path.write_text('{"version": "cua.policy/v1", "name": "x", "origins": ["http://h:${NOPE}"]}', encoding="utf-8")
    with pytest.raises(PolicyError):
        load_policy(path, variables={"DOLIBARR_PORT": "1"})


def test_secrets_are_not_available_as_policy_variables():
    from cua.config import Settings

    assert Settings(dolibarr_admin_password="not-a-real-secret").policy_variables() == {"DOLIBARR_PORT": "8085"}


@pytest.mark.parametrize(
    "bad",
    [
        {"origins": ["http://*.example.com"]},
        {"origins": ["file:///etc"]},
        {"origins": ["http://localhost:8087/some/path"]},
        {"origins": []},
        {"max_effect": "write"},  # design-only, never accepted
        {"routes": {"allow": ["societe/**"]}},
        {"unexpected": 1},
    ],
)
def test_malformed_policy_is_refused_without_echoing_values(bad):
    with pytest.raises(ValueError):  # noqa: PT011 - pydantic ValidationError is a ValueError
        make_policy(**bad)


def test_loader_error_does_not_echo_values(tmp_path):
    path = tmp_path / "p.json"
    path.write_text('{"version": "cua.policy/v1", "name": "x", "origins": ["http://leaky-value-123"], "bogus": 1}',
                    encoding="utf-8")
    with pytest.raises(PolicyError) as e:
        load_policy(path, variables={})
    assert "leaky-value-123" not in str(e.value)


def test_policy_denies_by_default():
    policy = Policy(version="cua.policy/v1", name="empty", origins=[APP])
    assert not policy.check_url(f"{APP}/index.php").allowed  # no allow rules
    assert not policy.effect_within_ceiling("read")  # ceiling defaults to none
    with pytest.raises(PolicyDenied):
        policy.require_sandbox()


def test_require_sandbox_passes_only_for_sandbox_policies():
    make_policy(sandbox=True).require_sandbox()
    with pytest.raises(PolicyDenied) as e:
        make_policy(sandbox=False).require_sandbox()
    assert e.value.decision.rule == "sandbox_only"


# ---------------------------------------------------------------------------------------------------- the guard


def test_guard_allows_a_request_inside_every_rule(guard):
    decision = guard.check(request())
    assert decision.allowed and decision.rule == "all_rules_passed"


def test_guard_denies_action_type(guard):
    decision = guard.check(request(kind="select"))
    assert not decision.allowed and decision.rule == "action_type"
    assert guard.check(request(kind="type")).allowed  # same request shape, allowed kind


@pytest.mark.parametrize(
    "page_url",
    [
        "http://localhost:8085/index.php",  # right host, other target's port
        "http://evil.example/index.php",
        "https://localhost:8087/index.php",  # scheme differs
        "http://localhost:8087@evil.example/index.php",  # userinfo trick
        "file:///C:/Windows/win.ini",
        "data:text/html,<h1>x</h1>",
        "about:blank",
        "",
        "not a url",
    ],
)
def test_guard_denies_page_urls_outside_the_origin_allowlist(guard, page_url):
    decision = guard.check(request(page_url=page_url))
    assert not decision.allowed and decision.rule in {"origin_allowlist", "scheme"}


def test_guard_denies_navigation_to_other_origins_and_paths(guard):
    assert guard.check(request(kind="navigate", target_url=f"{APP}/societe/list.php")).allowed
    off_origin = guard.check(request(kind="navigate", target_url="http://evil.example/"))
    assert off_origin.rule == "origin_allowlist" and not off_origin.allowed
    off_path = guard.check(request(kind="navigate", target_url=f"{APP}/admin/index.php"))
    assert off_path.rule == "path_rules" and not off_path.allowed
    no_target = guard.check(request(kind="navigate"))
    assert no_target.rule == "navigate_target" and not no_target.allowed


def test_link_click_target_is_checked_too(guard):
    decision = guard.check(request(kind="click", target_url="http://evil.example/steal"))
    assert not decision.allowed and decision.rule == "origin_allowlist"


@pytest.mark.parametrize(
    ("path", "allowed"),
    [
        ("/index.php", True),
        ("/", True),
        ("/societe", True),  # `/dir/**` also matches `/dir`
        ("/societes", False),
        ("/societe/card.php", True),
        ("/societe/admin/secret.php", False),  # deny beats allow
        ("/SOCIETE/Admin/secret.php", False),  # deny is case-insensitive
        ("/societe/../admin/index.php", False),  # traversal is not comparable
        ("/societe/%2e%2e/admin/index.php", False),
        ("/societe%2fadmin/index.php", False),
        ("/societe//admin//x.php", False),  # doubled slashes collapse before matching
        ("/societe\\admin\\x.php", False),
        ("/other.php", False),
    ],
)
def test_path_rules(guard, path, allowed):
    assert guard.check(request(page_url=f"{APP}{path}")).allowed is allowed


def test_guard_enforces_the_effect_ceiling():
    guard = PolicyGuard(make_policy(max_effect="read"), Lease())
    assert guard.check(request(effect="none")).allowed
    assert guard.check(request(effect="read")).allowed
    over = guard.check(request(effect="commit"))
    assert not over.allowed and over.rule == "effect_ceiling"
    assert PolicyGuard(make_policy(max_effect="commit"), Lease()).check(request(effect="commit")).allowed
    assert not PolicyGuard(make_policy(max_effect="none"), Lease()).check(request(effect="read")).allowed


def test_stale_lease_generation_cannot_act(guard, lease):
    token = LeaseToken(holder=AUTOMATION, generation=1)
    assert guard.check(request(lease=token)).allowed
    lease.generation = 2  # a handoff happened since the decision was made
    decision = guard.check(request(lease=token))
    assert not decision.allowed and decision.rule == "lease_generation"
    assert guard.check(request(lease=LeaseToken(holder=AUTOMATION, generation=2))).allowed


def test_future_generation_is_also_denied(guard):
    decision = guard.check(request(lease=LeaseToken(holder=AUTOMATION, generation=9)))
    assert decision.rule == "lease_generation"


def test_automation_cannot_act_while_a_human_holds_the_lease():
    lease = Lease(holder=human("op1"), generation=2)
    guard = PolicyGuard(make_policy(), lease)
    # a token naming the human is the human's, not automation's: automated requests must not ride on it
    assert guard.check(request(lease=LeaseToken(holder=human("op1"), generation=2))).rule == "lease_holder"
    # automation's own (older) token no longer matches
    assert guard.check(request(lease=LeaseToken(holder=AUTOMATION, generation=1))).rule == "lease_generation"


def test_lease_provider_may_return_a_control_lease():
    lease = ControlLease(holder=AUTOMATION, generation=4, reason="start", since=datetime.now(UTC))
    guard = PolicyGuard(make_policy(), lambda: lease)
    assert guard.check(request(lease=lease.token())).allowed


def test_unreadable_lease_denies():
    def broken():
        raise RuntimeError("lease store down")

    decision = PolicyGuard(make_policy(), broken).check(request())
    assert not decision.allowed and decision.rule == "lease_unavailable"


def test_guard_never_raises_and_denies_what_it_cannot_evaluate(guard, monkeypatch):
    monkeypatch.setattr(Policy, "check_url", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    decision = guard.check(request())
    assert not decision.allowed and decision.rule == "guard_error"


def test_reasons_never_contain_input_values(guard):
    secret = "CU1910-00021-hunter2"
    for req in (
        request(page_url=f"http://evil.example/?customer={secret}"),
        request(page_url=f"{APP}/admin/{secret}.php"),
        request(kind="navigate", target_url=f"http://evil.example/{secret}?token={secret}"),
        request(kind="select", page_url=f"{APP}/index.php?q={secret}"),
        request(effect="commit", page_url=f"{APP}/index.php?q={secret}"),
    ):
        decision = guard.check(req)
        assert not decision.allowed
        assert secret not in decision.reason and secret not in decision.rule
        assert secret not in repr(decision)


def test_on_deny_hook_sees_denials_only_and_cannot_change_the_decision(lease):
    seen = []
    guard = PolicyGuard(make_policy(), lease, on_deny=lambda req, dec: seen.append((req.kind, dec.rule)))
    assert guard.check(request()).allowed
    assert seen == []
    assert not guard.check(request(kind="select")).allowed
    assert seen == [("select", "action_type")]

    def explode(req, dec):
        raise RuntimeError("log sink down")

    assert not PolicyGuard(make_policy(), lease, on_deny=explode).check(request(kind="select")).allowed


def test_policy_denied_carries_the_decision(guard):
    decision = guard.check(request(kind="select"))
    err = PolicyDenied(decision)
    assert err.decision is decision and "action_type" in str(err)
