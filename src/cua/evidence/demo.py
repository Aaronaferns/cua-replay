"""`cua demo up`: start the target, wait for health, reset to the seeded baseline, print the URL.

The scripts run where Docker runs: bash (WSL on Windows). Keep WSL alive on Windows: leave a WSL terminal open or run
`wsl -- sleep infinity` in a spare window; WSL stops an idle distribution and the container goes with it.
"""

from __future__ import annotations

import shutil
import subprocess

from cua.config import REPO_ROOT, settings

_DIR = REPO_ROOT / "target" / "dolibarr"


def cmd_demo_up(args) -> int:
    bash = shutil.which("bash")
    if bash is None:
        print("bash is required to run the target scripts (on Windows: Git Bash or run from WSL)")
        return 2
    for script, extra in (("up.sh", [str(args.timeout)]), ("reset.sh", [])):
        if subprocess.run([bash, (_DIR / script).as_posix(), *extra], cwd=REPO_ROOT).returncode:
            print(f"{script} failed (is Docker running? is the WSL distribution alive?)")
            return 1
    print(f"target ready: http://localhost:{settings().dolibarr_port}/index.php")
    print("Windows: keep WSL alive (open a WSL terminal, or run `wsl -- sleep infinity`) or the container stops.")
    return 0
