"""A port set in the Windows environment must reach the WSL scripts, and a script that acted on another compose
project than `settings()` names must fail loudly (the S3 incident: DOLIBARR_PORT=8087 reset project cua-8085)."""

from __future__ import annotations

import subprocess

import dolictl
import pytest

from cua.config import Settings


def test_wsl_env_forwards_the_port_and_the_env_file_when_set():
    env = dolictl.wsl_env({"DOLIBARR_PORT": "8087", "CUA_ENV_FILE": r"C:\x\.env", "PATH": "p"})
    names = env["WSLENV"].split(":")
    assert "DOLIBARR_PORT" in names
    assert "CUA_ENV_FILE/p" in names  # /p: WSL translates the Windows path


def test_wsl_env_leaves_out_what_is_not_set_and_keeps_existing_entries():
    env = dolictl.wsl_env({"DOLIBARR_PORT": "8087", "WSLENV": "FOO/u:DOLIBARR_PORT"}, "MYSQL_PWD")
    assert env["WSLENV"].split(":") == ["FOO/u", "DOLIBARR_PORT", "MYSQL_PWD"]  # no duplicate, nothing dropped
    assert dolictl.wsl_env({})["WSLENV"] == ""


def test_run_script_hands_the_forwarding_to_the_subprocess(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(cmd, 0, stdout="compose project: cua-8087\nok\n", stderr="")

    monkeypatch.setenv("DOLIBARR_PORT", "8087")
    monkeypatch.setattr(dolictl.subprocess, "run", fake_run)
    monkeypatch.setattr(dolictl, "load_settings", lambda: Settings(dolibarr_port=8087))
    out, _ = dolictl.run_script("reset.sh")
    assert "DOLIBARR_PORT" in seen["env"]["WSLENV"].split(":")
    assert seen["env"]["DOLIBARR_PORT"] == "8087"
    assert "ok" in out


def test_a_script_that_acted_on_another_project_fails_loudly(monkeypatch):
    monkeypatch.setattr(dolictl.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, stdout="compose project: cua-8085\nrestored\n", stderr=""))
    monkeypatch.setattr(dolictl, "load_settings", lambda: Settings(dolibarr_port=8087))
    with pytest.raises(dolictl.CtlError, match=r"cua-8085.*cua-8087"):
        dolictl.run_script("reset.sh")


def test_a_script_that_reports_no_project_fails(monkeypatch):
    monkeypatch.setattr(dolictl.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, stdout="restored\n", stderr=""))
    monkeypatch.setattr(dolictl, "load_settings", lambda: Settings(dolibarr_port=8087))
    with pytest.raises(dolictl.CtlError, match="did not report"):
        dolictl.run_script("reset.sh")


def test_check_project_accepts_the_matching_project():
    assert dolictl.check_project("up.sh", "x\ncompose project: cua-8085\n", Settings(dolibarr_port=8085)) == "cua-8085"
