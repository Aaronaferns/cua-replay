"""Evidence lint: `cua lint [--root evidence/]` scans text files for what must never ship.

Findings: a configured secret value (`.env`), an SSN, a Cookie / Set-Cookie header carrying a value, an e-mail address
not in the masked form `[REDACTED:email]` (the evidence has no reserved-domain addresses, so none are allowed).
The report names file, line and rule, never the matched text.
"""

from __future__ import annotations

import re
from pathlib import Path

from cua.config import settings

_RULES = (
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("cookie", re.compile(r"(?i)\b(?:set-)?cookie\b[\"']?\s*[:=]\s*[\"']?(?!\[REDACTED|\*|[\"']|$)\S")),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")),
)
_TEXT = {".json", ".jsonl", ".md", ".txt", ".html", ".log", ".yaml", ".yml", ".csv"}


def lint_tree(root: Path, secrets: list[str]) -> list[str]:
    """Findings as `path:line: rule`, sorted by path; secret values are matched but never reported."""
    secrets = [s for s in secrets if len(s) >= 4]
    out: list[str] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in _TEXT):
        if path.name == "evidence_lint.txt":
            continue
        rel = path.relative_to(root).as_posix()
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            rules = [name for name, rx in _RULES if rx.search(line)]
            if any(s in line for s in secrets):
                rules.append("secret")
            out += [f"{rel}:{n}: {r}" for r in rules]
    return out


def cmd_lint(args) -> int:
    root = Path(args.root)
    findings = lint_tree(root, settings().secret_values())
    report = "\n".join(findings + [f"{len(findings)} finding(s)" if findings else "clean"]) + "\n"
    (root / "evidence_lint.txt").write_text(report, encoding="utf-8", newline="\n")
    print(report, end="")
    return 1 if findings else 0
