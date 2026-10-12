"""Error hygiene: validation errors must not echo inputs (plan M4), and errors are re-messaged at the boundary."""

from __future__ import annotations

import importlib
import pkgutil

import pytest
from pydantic import BaseModel, SecretStr, ValidationError

import cua
from cua.config import Settings
from cua.safety.redact import Redactor, safe_error_message

# Models exempt from `hide_input_in_errors=True`, with the reason. These are not design exemptions: they are models
# owned by Agent A (src/cua/surface/**, outside Agent C's write boundary) that still need the one-line fix
# `hide_input_in_errors=True` in their `model_config`. They are listed here until A makes the change; the test below
# fails once an exempted model complies, so the entries cannot go stale.
_PENDING_OWNER_A = "owned by Agent A (surface); needs hide_input_in_errors=True in model_config (contract request)"
EXEMPT: dict[str, str] = {
    "cua.surface.base.Observation": _PENDING_OWNER_A,
    "cua.surface.base.PageFacts": _PENDING_OWNER_A,
    "cua.surface.login.LoginTargets": _PENDING_OWNER_A,
    "cua.surface.login.Target": _PENDING_OWNER_A,
    "cua.surface.web.WebScope": _PENDING_OWNER_A,
}


def _all_cua_models() -> list[type[BaseModel]]:
    for info in pkgutil.walk_packages(cua.__path__, prefix="cua."):
        importlib.import_module(info.name)
    seen: dict[str, type[BaseModel]] = {}

    def walk(cls: type[BaseModel]) -> None:
        for sub in cls.__subclasses__():
            if sub.__module__.split(".")[0] == "cua":
                seen.setdefault(f"{sub.__module__}.{sub.__qualname__}", sub)
            walk(sub)

    walk(BaseModel)
    return list(seen.values())


def test_pydantic_errors_hide_inputs():
    models = _all_cua_models()
    assert len(models) > 20  # the walk really found the models
    missing = sorted(
        f"{m.__module__}.{m.__qualname__}"
        for m in models
        if m.model_config.get("hide_input_in_errors") is not True
        and f"{m.__module__}.{m.__qualname__}" not in EXEMPT
    )
    assert not missing, "models without hide_input_in_errors=True (add it to model_config):\n  " + "\n  ".join(missing)


def test_exemptions_are_real_explained_and_still_needed():
    by_name = {f"{m.__module__}.{m.__qualname__}": m for m in _all_cua_models()}
    for name, reason in EXEMPT.items():
        assert name in by_name and reason.strip(), name
        assert by_name[name].model_config.get("hide_input_in_errors") is not True, f"{name} complies now: drop it"


def test_a_hidden_input_model_does_not_echo_the_value():
    with pytest.raises(ValidationError) as caught:
        Settings(dolibarr_port="hunter2-not-a-port")
    assert "hunter2" not in str(caught.value)


# ------------------------------------------------------------------------------------------- safe_error_message


class _Model(BaseModel):
    model_config = {"hide_input_in_errors": False}  # worst case: the model does echo the input

    port: int
    secret: SecretStr = SecretStr("x")


def test_validation_errors_show_locations_and_types_only():
    with pytest.raises(ValidationError) as caught:
        _Model(port="hunter2-value", secret=object())
    assert "hunter2-value" in str(caught.value)  # the premise: the raw error leaks
    message = safe_error_message(caught.value)
    assert "hunter2" not in message
    assert message.startswith("ValidationError in _Model: ")
    assert "port (int_parsing)" in message


def test_foreign_exceptions_are_reduced_to_their_type():
    assert safe_error_message(KeyError("customer CU2601-00901")) == "KeyError"
    assert safe_error_message(ValueError("password=hunter2")) == "ValueError"


def test_our_own_exceptions_keep_their_message_but_are_redacted_and_bounded():
    from cua.evidence.writer import EvidenceError

    class Ours(Exception):
        __module__ = "cua.somewhere"

    assert safe_error_message(EvidenceError("image bytes are not a PNG")) == "EvidenceError: image bytes are not a PNG"
    redactor = Redactor(["CU2601-00901"])
    assert "CU2601" not in safe_error_message(Ours("code CU2601-00901\nfailed"), redactor=redactor)
    assert "\n" not in safe_error_message(Ours("a\nb"))
    assert "hunter2" not in safe_error_message(Ours("sent password=hunter2"))
    assert len(safe_error_message(Ours("x" * 5000))) <= 300


def test_the_cause_chain_is_not_followed():
    try:
        try:
            raise KeyError("secret-value-1234")
        except KeyError as inner:
            raise RuntimeError("outer") from inner
    except RuntimeError as e:
        assert safe_error_message(e) == "RuntimeError"

