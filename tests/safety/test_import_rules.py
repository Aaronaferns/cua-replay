"""docs/contracts.md import rules for the packages this owner writes: safety -> artifact, control.lease;
evidence -> safety, control.lease. Neither may reach surface, replay, agent, compiler, faults or anthropic."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "cua"
ALLOWED = {
    "safety": {"cua.artifact", "cua.control.lease", "cua.config", "cua.safety"},
    "evidence": {"cua.safety", "cua.control.lease", "cua.config", "cua.evidence"},
}
FORBIDDEN_THIRD_PARTY = {"anthropic", "fastapi", "uvicorn"}


def imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


@pytest.mark.parametrize("package", sorted(ALLOWED))
def test_package_imports_only_what_the_rules_allow(package):
    files = sorted((SRC / package).rglob("*.py"))
    assert files
    for path in files:
        for module in imports(path):
            top = module.split(".")[0]
            assert top not in FORBIDDEN_THIRD_PARTY, (path.name, module)
            if top == "cua":
                assert any(module == a or module.startswith(a + ".") for a in ALLOWED[package]), (path.name, module)


def test_safety_never_imports_faults():
    for path in (SRC / "safety").rglob("*.py"):
        assert not any(m.startswith("cua.faults") for m in imports(path))
