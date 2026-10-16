"""Evidence lint: a leaky tree is flagged without echoing values; the committed evidence is clean."""

from __future__ import annotations

import re
from pathlib import Path

from cua.cli import build_parser, main
from cua.config import settings
from cua.safety.lint import lint_tree

ROOT = Path(__file__).resolve().parents[2]
SECRET = "s3cr3t-value-XYZ"


def test_leaky_tree_is_flagged(tmp_path):
    (tmp_path / "a.json").write_text(
        f'{{"pw": "{SECRET}"}}\nssn 123-45-6789\nCookie: PHPSESSID=abc\nmail bob@acme.test\n', encoding="utf-8"
    )
    found = lint_tree(tmp_path, [SECRET])
    assert sorted(f.rsplit(": ", 1)[1] for f in found) == ["cookie", "email", "secret", "ssn"]
    assert not any(SECRET in f for f in found)


def test_masked_tree_is_clean(tmp_path):
    (tmp_path / "a.json").write_text(
        'mail [REDACTED:email]\nssn ***-**-6789\nCookie: [REDACTED]\n{"Set-Cookie": "[REDACTED]"}\n', encoding="utf-8"
    )
    assert lint_tree(tmp_path, [SECRET]) == []


def test_cli_lint_writes_report_and_exits_nonzero(tmp_path):
    (tmp_path / "a.txt").write_text("123-45-6789\n", encoding="utf-8")
    assert main(["lint", "--root", str(tmp_path)]) == 1
    assert (tmp_path / "evidence_lint.txt").read_text(encoding="utf-8").startswith("a.txt:1: ssn")


def test_committed_evidence_is_clean():
    assert lint_tree(ROOT / "evidence", settings().secret_values()) == []


def test_readme_commands_name_real_subcommands():
    subs = set(build_parser()._subparsers._group_actions[0].choices)
    text = "".join((ROOT / f).read_text(encoding="utf-8") for f in ("README.md", "evidence/README.md"))
    cmds = set(re.findall(r"uv run cua ([a-z-]+)", text))
    assert cmds and cmds <= subs
