"""`cua` command line.

Each package that offers commands owns a `commands.py` with `register(subparsers)`; this file only wires them up,
so agents add commands without editing a shared file:

    cua.agent.commands     discover                                    (Agent A)
    cua.replay.commands    compile, replay, validate, capabilities, approve (Agent B)
    cua.control.commands   console                                     (Agent C)
    cua.evidence.commands  make-evidence, lint, demo, view, export-otel (Agent C)
"""

from __future__ import annotations

import argparse
import importlib
import sys

COMMAND_MODULES = (
    "cua.agent.commands",
    "cua.replay.commands",
    "cua.control.commands",
    "cua.evidence.commands",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cua", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in COMMAND_MODULES:
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as e:
            if e.name == name:  # that package has no commands yet
                continue
            raise
        module.register(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
