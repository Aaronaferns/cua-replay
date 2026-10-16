import argparse

import cua.evidence.commands as commands
from cua.evidence.demo import cmd_demo_up


def test_demo_up_runs_up_through_dolictl_then_resets(monkeypatch, capsys):
    commands._target_tools()
    import dolictl

    calls = []
    monkeypatch.setattr(dolictl, "run_script", lambda name, *a, **kw: calls.append((name, a)) or ("", 0.0))
    monkeypatch.setattr(commands, "_reset_target", lambda: calls.append(("reset", ())))
    assert cmd_demo_up(argparse.Namespace(timeout=5)) == 0
    assert calls == [("up.sh", ("5",)), ("reset", ())]
    assert "target ready: http://localhost:" in capsys.readouterr().out


def test_demo_up_reports_a_failed_script(monkeypatch, capsys):
    commands._target_tools()
    import dolictl

    def boom(*a, **kw):
        raise dolictl.CtlError("up.sh exited 1")

    monkeypatch.setattr(dolictl, "run_script", boom)
    assert cmd_demo_up(argparse.Namespace(timeout=5)) == 1
    assert "demo up failed" in capsys.readouterr().out
