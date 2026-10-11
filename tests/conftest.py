"""Shared test configuration. Changes go through the orchestrator (every agent's tests load this file).

Markers (see pyproject.toml):
  live  - needs the Dolibarr target; skipped when it is not reachable
  model - calls a real model API; skipped without ANTHROPIC_API_KEY
  human - needs a person at the headed browser; skipped unless CUA_HUMAN=1
"""

from __future__ import annotations

import os
import urllib.request

import pytest

from cua.config import settings


def _target_up() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{settings().dolibarr_port}/index.php", timeout=3) as r:
            return r.status == 200
    except OSError:
        return False


def pytest_collection_modifyitems(config, items):
    target = None
    for item in items:
        if "live" in item.keywords:
            if target is None:
                target = _target_up()
            if not target:
                item.add_marker(pytest.mark.skip(reason="Dolibarr target not reachable"))
        if "model" in item.keywords and settings().anthropic_api_key is None:
            item.add_marker(pytest.mark.skip(reason="ANTHROPIC_API_KEY not set"))
        if "human" in item.keywords and os.environ.get("CUA_HUMAN") != "1":
            item.add_marker(pytest.mark.skip(reason="needs a person (set CUA_HUMAN=1)"))
