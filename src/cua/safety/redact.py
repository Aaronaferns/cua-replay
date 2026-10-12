"""Redaction: what may be written to disk, a log or a model prompt.

Owner: Agent C. Two layers, both applied by `Redactor.text` / `Redactor.data`:

1. Known values: every configured secret (`Settings.secret_values()`) and every value a run supplies (capability
   inputs, typed text). Each value is matched in its raw form and in the encodings it takes in transit or in a page:
   URL-encoded (quote and quote_plus, one and two levels), HTML-escaped, JSON-escaped (with and without
   `\\uXXXX`, and PHP's `\\/`). Matching is case-insensitive, which also covers percent-encoding hex case.
   A value can carry a partial mask (`keep_last`): `****4242` instead of `[REDACTED:label]`.
2. Patterns for things that are sensitive without being known in advance: Cookie / Set-Cookie / Authorization
   headers, Bearer and Basic credentials, session ids, CSRF and API-token parameters (URL, form field, JSON), SSN-like
   numbers (masked to the last four) and e-mail addresses.

Limits, stated plainly: values shorter than `min_length` (default 4) are not matched as substrings because that would
mangle ordinary text; a secret re-encoded in a way not listed above (for example base64 of a larger string) is only
caught by the header patterns; pixels are not redacted here (screenshot masking happens at the surface).
"""

from __future__ import annotations

import html
import json
import re
import threading
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, quote_plus

from pydantic import SecretStr

if TYPE_CHECKING:
    from cua.config import Settings

REDACTED = "[REDACTED]"

# JSON/dict keys whose value is replaced wholesale (matched case-insensitively with `-` and `_` removed). A bare
# "token" is deliberately absent: replay events carry input *tokens* such as {{customer_code}}, which are evidence.
_SENSITIVE_KEYS = frozenset(
    {
        "password", "passwd", "pwd", "secret", "newtoken", "csrftoken", "xcsrftoken", "xxsrftoken", "accesstoken",
        "refreshtoken", "idtoken", "apikey", "xapikey", "dolapikey", "authorization", "proxyauthorization", "cookie",
        "setcookie", "cookies", "sessionid", "phpsessid",
    }
)  # fmt: skip

_TOKEN_NAMES = (
    r"(?:new_?)?token|csrf_?token|_token|csrfmiddlewaretoken|api_?key|dolapikey|access_token|id_token|refresh_token|"
    r"password|passwd|pwd|secret"
)
_HEADER_NAMES = r"set-cookie|cookie|authorization|proxy-authorization|x-api-key|dolapikey|x-csrf-token|x-xsrf-token"

_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "header",
        re.compile(rf"(?i)\b({_HEADER_NAMES})([\"']?\s*:\s*)[^\r\n]*"),
        rf"\g<1>\g<2>{REDACTED}",
    ),
    ("bearer", re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}"), rf"\g<1> {REDACTED}"),
    (
        "session_id",
        re.compile(r"(?i)\b(PHPSESSID|DOLSESSID_[0-9A-Za-z]+|session_?id)=[^;&\s\"'<>]+"),
        rf"\g<1>={REDACTED}",
    ),
    (
        "token_param",
        re.compile(rf"(?i)(?<![A-Za-z0-9])({_TOKEN_NAMES})=([^&\s\"'<>#;]+)"),
        rf"\g<1>={REDACTED}",
    ),
    (
        "token_field",
        re.compile(
            r"(?i)(<input\b[^>]*?\bname=[\"'](?:newtoken|token|csrf[_-]?token|_token)[\"'][^>]*?\bvalue=[\"'])"
            r"[^\"']*"
        ),
        rf"\g<1>{REDACTED}",
    ),
    (
        "token_field",
        re.compile(
            r"(?i)(<input\b[^>]*?\bvalue=[\"'])[^\"']*([\"'][^>]*?\bname=[\"']"
            r"(?:newtoken|token|csrf[_-]?token|_token)[\"'])"
        ),
        rf"\g<1>{REDACTED}\g<2>",
    ),
    (
        "token_json",
        re.compile(rf"(?i)(\"(?:{_TOKEN_NAMES})\"\s*:\s*\")(?:[^\"\\]|\\.)*(\")"),
        rf"\g<1>{REDACTED}\g<2>",
    ),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-(\d{4})\b"), r"***-**-\g<1>"),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"), "[REDACTED:email]"),
]
PATTERN_NAMES = tuple(dict.fromkeys(name for name, _, _ in _PATTERNS))


def _variants(value: str) -> set[str]:
    """The forms a value takes in URLs, HTML and JSON."""
    out = {value}
    for q in (quote(value, safe=""), quote(value), quote_plus(value), quote_plus(value, safe="")):
        out.add(q)
        out.add(quote(q, safe=""))  # double encoding (a redirect target carried in a query string)
    out.add(html.escape(value, quote=True))
    out.add(html.escape(value, quote=False))
    for ensure_ascii in (True, False):
        encoded = json.dumps(value, ensure_ascii=ensure_ascii)[1:-1]
        out.add(encoded)
        out.add(encoded.replace("/", "\\/"))  # PHP json_encode
    return {v for v in out if v}


def _mask(value: str, keep_last: int) -> str:
    return "*" * max(len(value) - keep_last, 4) + value[-keep_last:]


class Redactor:
    """Redacts known values and sensitive patterns from text and JSON-like data. Thread-safe; values can be added
    while a run is in flight (each new typed value becomes known)."""

    def __init__(self, values: Iterable[str | SecretStr] = (), *, min_length: int = 4):
        self._min_length = min_length
        self._lock = threading.Lock()
        self._replacements: dict[str, str] = {}  # lowercased variant -> replacement
        self._known: re.Pattern[str] | None = None
        self._count = 0
        for value in values:
            self.add(value)

    @classmethod
    def from_settings(cls, settings: Settings | None = None, extra: Iterable[str | SecretStr] = ()) -> Redactor:
        if settings is None:
            from cua.config import settings as load_settings

            settings = load_settings()
        redactor = cls()
        for secret in settings.secret_values():
            redactor.add(secret, label="secret")
        for value in extra:
            redactor.add(value, label="input")
        return redactor

    def __repr__(self) -> str:  # never show what it guards
        return f"Redactor(known_values={self._count})"

    @property
    def known_value_count(self) -> int:
        return self._count

    def add(self, value: str | SecretStr, *, label: str = "input", keep_last: int = 0) -> bool:
        """Register a value to redact everywhere. Returns False (and registers nothing) when it is shorter than
        `min_length`. `keep_last=n` replaces it with a mask that still shows its last n characters (needs len >= 3n)."""
        raw = value.get_secret_value() if isinstance(value, SecretStr) else str(value)
        if len(raw) < self._min_length:
            return False
        if keep_last < 0 or (keep_last and len(raw) < 3 * keep_last):
            raise ValueError("keep_last must be 0 or at most a third of the value's length")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,24}", label):
            raise ValueError("label must be a short identifier")
        replacement = _mask(raw, keep_last) if keep_last else f"[REDACTED:{label}]"
        with self._lock:
            for variant in _variants(raw):
                self._replacements.setdefault(variant.lower(), replacement)
            alternation = "|".join(re.escape(v) for v in sorted(self._replacements, key=len, reverse=True))
            self._known = re.compile(alternation, re.IGNORECASE)
            self._count += 1
        return True

    def add_many(self, values: Iterable[str | SecretStr], *, label: str = "input") -> None:
        for value in values:
            self.add(value, label=label)

    def _known_only(self, text: str) -> str:
        known, replacements = self._known, self._replacements
        if known is None:
            return text
        return known.sub(lambda m: replacements.get(m.group(0).lower(), REDACTED), text)

    def text(self, text: str, *, known_only: bool = False) -> str:
        """Redact a string. `known_only` skips the patterns (used on serialized JSON, where they could cut syntax)."""
        out = self._known_only(text)
        if known_only:
            return out
        for _name, pattern, replacement in _PATTERNS:
            out = pattern.sub(replacement, out)
        return out

    def data(self, obj: Any) -> Any:
        """Redact a JSON-like structure (dict/list/str/number/None), returning a new one. Values under sensitive keys
        are replaced wholesale; every string, key included, goes through `text`."""
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, Mapping):
            out: dict[str, Any] = {}
            for key, value in obj.items():
                key_text = self.text(str(key))
                if key_text.replace("-", "").replace("_", "").lower() in _SENSITIVE_KEYS and value not in (None, ""):
                    out[key_text] = REDACTED
                else:
                    out[key_text] = self.data(value)
            return out
        if isinstance(obj, (list, tuple, set, frozenset)):
            return [self.data(v) for v in obj]
        if isinstance(obj, (int, float)) and not isinstance(obj, bool):
            shown = str(obj)
            redacted = self._known_only(shown)
            return obj if redacted == shown else redacted
        return obj

    def contains_known(self, text: str) -> bool:
        """True if a known value (any variant) is present: for tests and the evidence lint."""
        return self._known is not None and self._known.search(text) is not None
