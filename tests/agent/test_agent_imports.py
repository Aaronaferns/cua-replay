"""Import rules: the action schema and the loop never pull in the model SDK; replay never reaches the agent."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import cua.agent


def run(code: str) -> str:
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_agent_package_init_is_empty():
    assert Path(cua.agent.__file__).read_text(encoding="utf-8").strip() == ""


def test_importing_actions_does_not_load_the_model_sdk():
    assert run("import sys, cua.agent.actions; print('anthropic' in sys.modules)") == "False"


def test_loop_and_scripted_adapter_do_not_load_the_model_sdk():
    code = ("import sys, cua.agent.loop, cua.agent.scripted_adapter, cua.agent.executor, cua.agent.model, "
            "cua.agent.discovery; print('anthropic' in sys.modules)")
    assert run(code) == "False"


def test_only_the_anthropic_adapter_imports_the_sdk():
    assert run("import sys, cua.agent.anthropic_adapter; print('anthropic' in sys.modules)") == "True"
    src = Path(cua.agent.__file__).parent
    offenders = [p.name for p in src.glob("*.py") if p.name != "anthropic_adapter.py"
                 and "import anthropic" in p.read_text(encoding="utf-8")]
    assert offenders == []
