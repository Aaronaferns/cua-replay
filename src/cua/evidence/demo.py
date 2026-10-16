"""`cua demo up`: start the target, wait for health, reset to the seeded baseline, print the URL.

The scripts run where Docker runs: bash (WSL on Windows). Keep WSL alive on Windows: leave a WSL terminal open or run
`wsl -- sleep infinity` in a spare window; WSL stops an idle distribution and the container goes with it.
"""

from __future__ import annotations

from cua.config import settings


def cmd_demo_up(args) -> int:
    from cua.evidence.commands import _reset_target, _target_tools

    _target_tools()
    import dolictl  # routes the scripts through WSL on Windows, where Docker runs

    try:
        dolictl.run_script("up.sh", str(args.timeout), timeout=args.timeout + 60)
        _reset_target()
    except (dolictl.CtlError, OSError) as e:
        print(f"demo up failed (is Docker running? is the WSL distribution alive?): {e}")
        return 1
    print(f"target ready: http://localhost:{settings().dolibarr_port}/index.php")
    print("Windows: keep WSL alive (open a WSL terminal, or run `wsl -- sleep infinity`) or the container stops.")
    return 0
