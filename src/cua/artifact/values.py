"""Typed values at the capability boundary: checking inputs and parsing displayed text into outputs.

Values cross the boundary as strings: decimals are exact decimal strings ("75.37"), integers digits, dates ISO 8601
(YYYY-MM-DD). Nothing here ever puts a value in an error message: callers log the field name only.

Leaf module: standard library plus the schema in this package.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from cua.artifact.names import display_text
from cua.artifact.schema import FieldSpec, NumberFormat

_DECIMAL = re.compile(r"^[+-]?\d+(\.\d+)?$")
_INTEGER = re.compile(r"^[+-]?\d+$")
_NUMBER_TOKEN = re.compile(r"[+-]?\d(?:[\d' ., ]*\d)?")


class ValueRejected(ValueError):
    """A value does not satisfy its declared type. The message names the field's type, never the value."""


def check_input(spec: FieldSpec, value: str) -> None:
    """Raise `ValueRejected` unless `value` is a well-formed instance of the declared input type."""
    if not isinstance(value, str) or value != value.strip() or not value:
        raise ValueRejected(f"expected a non-empty {spec.type} without surrounding whitespace")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueRejected("control characters are not allowed")
    if spec.type == "decimal" and not _DECIMAL.fullmatch(value):
        raise ValueRejected("expected an exact decimal string")
    if spec.type == "integer" and not _INTEGER.fullmatch(value):
        raise ValueRejected("expected an integer string")
    if spec.type == "date":
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueRejected("expected an ISO 8601 date") from None
    if spec.pattern is not None and not re.fullmatch(spec.pattern, value):
        raise ValueRejected("does not match the declared pattern")


def parse_output(spec: FieldSpec, shown: str, number_format: NumberFormat | None = None) -> str:
    """Turn displayed text into the declared output type, or raise `ValueRejected`.

    Decimals and integers: exactly one number must be present in the text (a currency sign, label or icon around it
    is ignored); group separators are removed and the decimal separator becomes '.'.
    """
    fmt = number_format or NumberFormat()
    text = display_text(shown)
    if spec.type in ("decimal", "integer"):
        tokens = _NUMBER_TOKEN.findall(text)
        if len(tokens) != 1:
            raise ValueRejected(f"expected exactly one number, found {len(tokens)}")
        token = tokens[0]
        for sep in {fmt.group, " ", " ", "'"} - {fmt.decimal, ""}:
            token = token.replace(sep, "")
        token = token.replace(fmt.decimal, ".")
        if spec.type == "integer":
            if not _INTEGER.fullmatch(token):
                raise ValueRejected("expected an integer")
            result = str(int(token))
        else:
            if not _DECIMAL.fullmatch(token):
                raise ValueRejected("expected a decimal number")
            try:
                result = format(Decimal(token), "f")
            except InvalidOperation:
                raise ValueRejected("expected a decimal number") from None
    elif spec.type == "date":
        try:
            result = date.fromisoformat(text).isoformat()
        except ValueError:
            raise ValueRejected("expected an ISO 8601 date") from None
    else:
        result = text
    if spec.pattern is not None and not re.fullmatch(spec.pattern, result):
        raise ValueRejected("does not match the declared pattern")
    return result
