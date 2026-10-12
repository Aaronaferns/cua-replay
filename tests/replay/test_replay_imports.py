"""Replay is model-free by construction: it can only import what the contract allows, directly or transitively."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import cua.replay

REPLAY_DIR = Path(cua.replay.__file__).parent
ALLOWED_CUA = ("cua.artifact", "cua.surface", "cua.safety", "cua.control", "cua.evidence", "cua.replay")
FORBIDDEN_PREFIXES = (
    "cua.agent", "cua.compiler", "cua.faults", "anthropic", "openai", "httpx", "requests", "aiohttp", "fastapi",
    "uvicorn", "target",
)


def replay_modules() -> list[str]:
    return sorted(
        "cua.replay" + ("" if p.stem == "__init__" else f".{p.stem}")
        for p in REPLAY_DIR.glob("*.py")
    )


def test_replay_never_imports_model_code():
    mods = [m for m in replay_modules() if not m.endswith(".commands")]  # CLI wiring is allowed to import anything
    code = (
        "import importlib, json, sys\n"
        f"for m in {mods!r}:\n"
        "    importlib.import_module(m)\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    loaded = json.loads(out)
    leaked = sorted(m for m in loaded if any(m == p or m.startswith(p + ".") for p in FORBIDDEN_PREFIXES))
    assert not leaked, leaked
    assert "cua.replay.interpreter" in loaded and "cua.artifact.schema" in loaded  # the check really imported things


def test_replay_sources_import_only_allowed_packages():
    for path in sorted(REPLAY_DIR.glob("*.py")):
        if path.stem == "commands":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module]
            for mod in mods:
                if mod.split(".")[0] == "cua":
                    assert mod.startswith(ALLOWED_CUA), f"{path.name} imports {mod}"
                assert not any(mod == p or mod.startswith(p + ".") for p in FORBIDDEN_PREFIXES), (
                    f"{path.name} imports {mod}")


def test_replay_does_not_use_the_target_rest_api_or_models_by_name():
    for path in sorted(REPLAY_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8").lower()
        for word in ("api/index.php", "dolibarr_api_key", "anthropic", "messages.create"):
            assert word not in text, f"{path.name} mentions {word!r}"
