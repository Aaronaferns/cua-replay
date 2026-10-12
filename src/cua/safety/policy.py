"""Policy: the single automated-action gate.

Owner: Agent C. `ActionRequest`, `PolicyDecision`, `PolicyDenied` and `Guard` are shared types others code against;
`Policy`, `load_policy` and `PolicyGuard` implement them.

Every automated action (discovery, replay, handlers) is described as an `ActionRequest` and checked by the `Guard`
*before* the surface touches the UI. Unknown means deny. A denied action raises `PolicyDenied` and leaves the UI
untouched. Policy denial reaches callers as `failure` with category `policy` and `approvable`, never as its own status.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from cua.artifact.schema import Effect
from cua.control.lease import AUTOMATION, ControlLease, LeaseToken

ActionKind = Literal["click", "type", "select", "press", "navigate", "extract"]
Actor = Literal["agent", "replay", "handler"]


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    kind: ActionKind
    actor: Actor
    page_url: str  # where the action happens
    target_url: str | None = None  # navigate: destination; link clicks: href when known
    effect: Effect
    lease: LeaseToken
    approval_id: str | None = None  # single-use approval bound to (action, generation), commit only


class PolicyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    rule: str  # which rule decided, e.g. "origin_allowlist", "action_type", "effect_ceiling", "lease_generation"
    reason: str  # safe to log: never contains input values
    approvable: bool = False


class PolicyDenied(Exception):
    def __init__(self, decision: PolicyDecision):
        super().__init__(f"policy denied ({decision.rule}): {decision.reason}")
        self.decision = decision


class Guard(Protocol):
    def check(self, request: ActionRequest) -> PolicyDecision: ...


# ------------------------------------------------------------------------------------------------ the policy file

_EFFECT_RANK: dict[str, int] = {"none": 0, "read": 1, "commit": 2}
_DEFAULT_PORTS = {"http": 80, "https": 443}
_WEBSOCKET_SCHEMES = {"ws": "http", "wss": "https"}
_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class PolicyError(ValueError):
    """A policy file could not be loaded. Never partially applied: a bad policy is no policy."""


def _origin_key(url: str, *, websocket: bool = False) -> tuple[str, str, int] | None:
    """(scheme, host, port) of an absolute http(s) URL, or None for anything we do not understand."""
    try:
        parts = urlsplit(url.strip())
        scheme = parts.scheme.lower()
        if websocket:
            scheme = _WEBSOCKET_SCHEMES.get(scheme, scheme)
        host = parts.hostname
        if scheme not in _DEFAULT_PORTS or not host:
            return None
        if not (re.fullmatch(r"[a-z0-9.-]+", host.lower()) or re.fullmatch(r"[0-9a-f:.]+", host.lower())):
            return None  # wildcards and other non-hostname characters are never an origin
        if parts.username is not None or parts.password is not None:
            return None  # `http://allowed@evil/` style tricks: userinfo is never legitimate here
        port = parts.port or _DEFAULT_PORTS[scheme]
    except ValueError:
        return None
    return scheme, host.lower(), port


def _normalized_path(url: str) -> str | None:
    """The URL path as the policy sees it, or None when it is not safely comparable (traversal, encoded separators)."""
    try:
        raw = urlsplit(url.strip()).path or "/"
    except ValueError:
        return None
    lowered = raw.lower()
    if any(token in lowered for token in ("%2e", "%2f", "%5c", "%00")) or "\\" in raw:
        return None
    path = re.sub(r"/{2,}", "/", unquote(raw))
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in path):
        return None
    if any(segment in {".", ".."} for segment in path.split("/")):
        return None
    return path


@lru_cache(maxsize=512)
def _glob(pattern: str) -> re.Pattern[str]:
    """`**` crosses `/`, `*` does not, `?` is one non-`/` character. `/dir/**` also matches `/dir`."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("/**", i) and i + 3 == len(pattern):
            out.append(r"(?:/.*)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class PathRules(_Strict):
    """Path globs on an allowed origin. Deny beats allow; a path that matches no allow rule is denied."""

    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)

    @field_validator("allow", "deny")
    @classmethod
    def _rooted(cls, patterns: list[str]) -> list[str]:
        if any(not p.startswith("/") for p in patterns):
            raise ValueError("path patterns must start with '/'")
        return patterns


class Policy(_Strict):
    """What automation may do on a target. Loaded from `policies/*.json`; every field is closed and defaults deny.

    origins     the only origins the browser may talk to (http/https; ws/wss follow their http twin)
    routes      path allow/deny globs, applied on every allowed origin
    actions     the action kinds automation may perform
    max_effect  the highest effect class any action may declare (the capability's own ceiling is checked elsewhere)
    sandbox     true only for disposable test targets; fault injection must refuse to install unless it is true
    """

    version: Literal["cua.policy/v1"]
    name: str
    sandbox: bool = False
    origins: list[str] = Field(min_length=1)
    routes: PathRules = Field(default_factory=PathRules)
    actions: list[ActionKind] = Field(default_factory=list)
    max_effect: Effect = "none"

    @field_validator("origins")
    @classmethod
    def _origins_are_origins(cls, origins: list[str]) -> list[str]:
        normalized: list[str] = []
        for origin in origins:
            key = _origin_key(origin)
            parts = urlsplit(origin)
            if key is None or parts.path not in ("", "/") or parts.query or parts.fragment:
                raise ValueError("origins must be bare http(s) origins such as http://localhost:8087")
            normalized.append(f"{key[0]}://{key[1]}:{key[2]}")
        return normalized

    def _origin_keys(self) -> set[tuple[str, str, int]]:
        keys = {_origin_key(o) for o in self.origins}
        return {k for k in keys if k is not None}

    def check_url(self, url: str, *, websocket: bool = False) -> PolicyDecision:
        """Is the browser allowed to reach this URL? The reason never contains the URL."""
        key = _origin_key(url, websocket=websocket)
        if key is None:
            return PolicyDecision(allowed=False, rule="scheme", reason="url is not an absolute http(s) URL")
        if key not in self._origin_keys():
            return PolicyDecision(allowed=False, rule="origin_allowlist", reason="origin is not on the allowlist")
        path = _normalized_path(url)
        if path is None:
            return PolicyDecision(allowed=False, rule="path_rules", reason="path is not in a comparable form")
        if any(_glob(p.lower()).fullmatch(path.lower()) for p in self.routes.deny):
            return PolicyDecision(allowed=False, rule="path_rules", reason="path matches a deny rule")
        if not any(_glob(p).fullmatch(path) for p in self.routes.allow):
            return PolicyDecision(allowed=False, rule="path_rules", reason="path matches no allow rule")
        return PolicyDecision(allowed=True, rule="url_allowed", reason="origin and path are allowed")

    def effect_within_ceiling(self, effect: str) -> bool:
        rank = _EFFECT_RANK.get(effect)
        return rank is not None and rank <= _EFFECT_RANK[self.max_effect]

    def require_sandbox(self) -> None:
        """Fault injection calls this first: only a policy that marks the target as a sandbox permits it."""
        if not self.sandbox:
            raise PolicyDenied(
                PolicyDecision(allowed=False, rule="sandbox_only", reason="policy does not mark the target as sandbox")
            )


def _resolve(value: Any, variables: Mapping[str, str]) -> Any:
    if isinstance(value, str):

        def sub(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in variables:
                raise PolicyError(f"policy references unknown variable ${{{name}}}")
            return variables[name]

        resolved = _PLACEHOLDER.sub(sub, value)
        if "${" in resolved:
            raise PolicyError("policy contains a malformed placeholder")
        return resolved
    if isinstance(value, list):
        return [_resolve(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: _resolve(v, variables) for k, v in value.items()}
    return value


def policies_dir() -> Path:
    from cua.config import REPO_ROOT

    return REPO_ROOT / "policies"


def load_policy(source: str | Path, *, variables: Mapping[str, str] | None = None) -> Policy:
    """Load a policy by name (`"dolibarr-sandbox"` -> `policies/dolibarr-sandbox.json`) or by path.

    Placeholders: any string in the file may contain `${NAME}`, resolved at load from `variables` (default:
    `Settings.policy_variables()`, i.e. `${DOLIBARR_PORT}`, because the three targets run on 8085/8086/8087). An
    unknown placeholder is an error, secrets are not available as variables, and nothing is resolved after load.
    """
    path = Path(source)
    if isinstance(source, str) and not any(sep in source for sep in "/\\") and path.suffix != ".json":
        path = policies_dir() / f"{source}.json"
    if variables is None:
        from cua.config import settings

        variables = settings().policy_variables()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise PolicyError("policy file must be a JSON object")
        return Policy.model_validate(_resolve(raw, variables))
    except (OSError, json.JSONDecodeError) as e:
        raise PolicyError(f"cannot read policy {path.name}: {type(e).__name__}") from None
    except ValidationError as e:
        fields = sorted({".".join(str(p) for p in err["loc"]) for err in e.errors()})
        raise PolicyError(f"invalid policy {path.name}: bad fields {fields}") from None


# --------------------------------------------------------------------------------------------------- the guard

LeaseProvider = Callable[[], "LeaseToken | ControlLease"]


class PolicyGuard:
    """The `Guard` for a `Policy`. Every automated action goes through `check` before the UI is touched.

    `lease` returns the *current* lease (a `LeaseToken` or `ControlLease`); the request's token must equal it
    exactly and name the automation as holder. Anything the guard cannot evaluate is denied, never raised.
    Checks run in order: lease, action kind, page URL, target URL, effect ceiling; the first failure decides.
    """

    def __init__(self, policy: Policy, lease: LeaseProvider):
        self.policy = policy
        self._lease = lease

    def check(self, request: ActionRequest) -> PolicyDecision:
        try:
            return self._check(request)
        except Exception:  # noqa: BLE001 - a guard that crashes must deny, not propagate
            return PolicyDecision(allowed=False, rule="guard_error", reason="request could not be evaluated")

    def _check(self, request: ActionRequest) -> PolicyDecision:
        try:
            current = self._lease()
            current = current.token() if isinstance(current, ControlLease) else current
        except Exception:  # noqa: BLE001
            return self._deny("lease_unavailable", "the current lease could not be read")
        if request.lease.holder != current.holder or request.lease.generation != current.generation:
            return self._deny("lease_generation", "lease token does not match the current lease")
        if current.holder != AUTOMATION:
            return self._deny("lease_holder", "automation does not hold the lease")

        if request.kind not in self.policy.actions:
            return self._deny("action_type", "action kind is not allowed by the policy")

        page = self.policy.check_url(request.page_url)
        if not page.allowed:
            return page
        if request.kind == "navigate" and request.target_url is None:
            return self._deny("navigate_target", "navigation has no destination to check")
        if request.target_url is not None:
            target = self.policy.check_url(request.target_url)
            if not target.allowed:
                return target

        if not self.policy.effect_within_ceiling(request.effect):
            return self._deny("effect_ceiling", "effect exceeds the policy ceiling")
        return PolicyDecision(allowed=True, rule="all_rules_passed", reason="allowed")

    @staticmethod
    def _deny(rule: str, reason: str) -> PolicyDecision:
        return PolicyDecision(allowed=False, rule=rule, reason=reason)
