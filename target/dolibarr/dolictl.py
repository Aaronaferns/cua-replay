"""Run the target's shell scripts and database commands from Python on Windows (docker lives in WSL2) or Linux.

Used by `seed.py` (rights and the admin key fallback need the database) and by tests (reset, CAPTCHA toggle).
The database password travels in the `MYSQL_PWD` environment variable (forwarded into WSL with `WSLENV`), never on a
command line, and the SQL goes through stdin, so nothing needs shell quoting.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from cua.config import Settings

HERE = Path(__file__).resolve().parent
DUMP_FILE = HERE / "dumps" / "post-seed.sql"  # the same path reset.sh uses
WSL_DISTRO = os.environ.get("CUA_WSL_DISTRO", "Ubuntu")


class CtlError(Exception):
    """A script or database command failed. Carries the command name and exit code, not its output."""


def _wsl() -> list[str] | None:
    if os.name == "nt" and shutil.which("wsl"):
        return ["wsl", "-d", WSL_DISTRO, "--"]
    return None


def to_wsl_path(path: Path) -> str:
    s = str(path.resolve())
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", s)
    if not m:
        return s.replace("\\", "/")
    return f"/mnt/{m.group(1).lower()}/{m.group(2).replace(chr(92), '/')}"


def _docker() -> list[str]:
    wsl = _wsl()
    if wsl is not None:
        return [*wsl, "docker"]
    if shutil.which("docker"):
        return ["docker"]
    raise CtlError("docker is not available (on Windows it lives in WSL2: install/start the Ubuntu distro)")


def run_script(name: str, *args: str, timeout: float = 900.0) -> tuple[str, float]:
    """Run target/dolibarr/<name> with bash; returns (stdout, seconds). Raises CtlError on a non-zero exit."""
    wsl = _wsl()
    script = to_wsl_path(HERE / name) if wsl else str(HERE / name)
    started = time.perf_counter()
    proc = subprocess.run([*(wsl or []), "bash", script, *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)
    elapsed = time.perf_counter() - started
    if proc.returncode != 0:
        raise CtlError(f"{name} {' '.join(args)} exited {proc.returncode}: {proc.stderr.strip()[-400:]}")
    return proc.stdout, elapsed


def sql(settings: Settings, statements: str, *, timeout: float = 120.0) -> str:
    """Run SQL as the database root user in the project's db container; returns the tab-separated result."""
    if settings.dolibarr_db_root_password is None:
        raise CtlError("DOLIBARR_DB_ROOT_PASSWORD is not set")
    env = dict(os.environ)
    env["MYSQL_PWD"] = settings.dolibarr_db_root_password.get_secret_value()
    env["WSLENV"] = ":".join(filter(None, [env.get("WSLENV"), "MYSQL_PWD"]))
    container = f"cua-{settings.dolibarr_port}-db-1"
    proc = subprocess.run([*_docker(), "exec", "-i", "-e", "MYSQL_PWD", container, "mariadb", "-uroot", "dolidb", "-N"],
                          input=statements, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env=env, timeout=timeout)
    if proc.returncode != 0:
        # the server's message names tables and columns, not the password; the statements are ours
        raise CtlError(f"sql failed: {proc.stderr.strip()[-300:]}")
    return proc.stdout
