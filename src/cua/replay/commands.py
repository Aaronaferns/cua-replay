"""`cua replay` and `cua compile`.

    cua replay <app>.<capability> [--version 1.0.0] --input k=v ... [--headed] [--json]
    cua compile <trace.json> [--profile apps/dolibarr/profile.json] --name <app.capability> --out <path>

Exit codes (replay): 0 success, 1 failure, 2 the command itself could not run (bad arguments, artifact not found or
invalid, session could not open), 3 business outcome, 4 indeterminate. Approval gating arrives with M3: a draft runs
with a warning on stderr.

This is CLI wiring, not replay logic: it is the one module in this package allowed to open a browser session
(through `cua.control.runtime`, imported lazily). It still imports no model code. `compile` imports the compiler
lazily, inside the command.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
from pathlib import Path
from typing import Any

from cua.artifact.lock import load_capability, load_profile
from cua.artifact.schema import Capability
from cua.artifact.validate import validate_capability
from cua.config import REPO_ROOT
from cua.replay.interpreter import Credentials, replay
from cua.replay.result import RunResult

EXIT_SUCCESS, EXIT_FAILURE, EXIT_USAGE, EXIT_BUSINESS_OUTCOME, EXIT_INDETERMINATE = 0, 1, 2, 3, 4
EXIT_CODES = {
    "success": EXIT_SUCCESS,
    "failure": EXIT_FAILURE,
    "business_outcome": EXIT_BUSINESS_OUTCOME,
    "indeterminate": EXIT_INDETERMINATE,
}

_NAME = re.compile(r"([a-z][a-z0-9_-]*)\.([a-z][a-z0-9_]*)")
_VERSIONED = re.compile(r"(?P<cap>.+)@(?P<ver>\d+\.\d+\.\d+)(?P<draft>\.draft)?\.json")


class CommandError(Exception):
    """The command could not run; the message is for the person at the terminal."""


def capabilities_dir() -> Path:
    return REPO_ROOT / "capabilities"


def _split_name(spec: str) -> tuple[str, str]:
    m = _NAME.fullmatch(spec)
    if m is None:
        raise CommandError(f"capability must look like <app>.<capability> (for example dolibarr.lookup_x), got {spec!r}")
    return m.group(1), m.group(2)


def _version_key(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


def find_capability_file(spec: str, version: str | None, root: Path | None = None) -> tuple[Path, bool]:
    """(path, is_draft). `<capability>@<version>.json` wins over `.draft.json`; without a version the highest wins."""
    app, cap = _split_name(spec)
    folder = (root or capabilities_dir()) / app
    found: dict[str, dict[bool, Path]] = {}
    for path in sorted(folder.glob(f"{cap}@*.json")) if folder.is_dir() else []:
        m = _VERSIONED.fullmatch(path.name)
        if m is not None and m.group("cap") == cap:
            found.setdefault(m.group("ver"), {})[m.group("draft") is not None] = path
    if version is not None:
        found = {v: files for v, files in found.items() if v == version}
    if not found:
        wanted = f"{cap}@{version or '<version>'}"
        raise CommandError(f"no capability file {wanted}.json or {wanted}.draft.json under {folder}")
    best = max(found, key=_version_key)
    files = found[best]
    return (files[False], False) if False in files else (files[True], True)


def parse_inputs(pairs: list[str]) -> dict[str, str]:
    inputs: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise CommandError(f"--input expects key=value, got {pair!r}")
        if key in inputs:
            raise CommandError(f"--input {key} was given twice")
        inputs[key] = value
    return inputs


def _open_session(**kwargs: Any) -> Any:
    from cua.control.runtime import open_session  # lazy: the runtime pulls in the browser

    return open_session(**kwargs)


def render(result: RunResult) -> str:
    """A short, value-free summary for people; `--json` prints the whole result."""
    lines = [f"{result.capability}@{result.version}  {result.status}  ({result.outcome})  run {result.run_id}"]
    for name, value in result.outputs.items():
        lines.append(f"  {name} = {value}")
    if result.failure is not None:
        f = result.failure
        where = f" at {f.step_id}" if f.step_id else ""
        lines.append(f"  {f.category}/{f.code}{where}: {f.detail}")
        if f.observed is not None:
            lines.append(f"  observed state {f.observed.state} on {f.observed.url}")
            if f.observed.near_misses:
                lines.append("  closest: " + ", ".join(f.observed.near_misses))
    if result.side_effects.commit_state != "none":
        lines.append(f"  commit: {result.side_effects.commit_state}; retry safe: {result.retry_safe}")
    for d in result.drift:
        lines.append(f"  drift at {d.step_id}: {d.target} used {d.used_strategy}")
    return "\n".join(lines)


async def run_replay(args: argparse.Namespace) -> tuple[RunResult | None, int]:
    path, draft = find_capability_file(args.capability, args.version)
    if draft:
        print(f"warning: {path.name} is an unapproved draft; approval gating is not enforced yet", file=sys.stderr)
    capability: Capability = load_capability(path)
    inputs = parse_inputs(args.input)
    app = capability.app
    profile_path = REPO_ROOT / "apps" / app / "profile.json"
    run_id = args.run_id or f"replay-{time.strftime('%Y%m%d-%H%M%S')}-{capability.id.split('.')[-1][:24]}"

    async with _open_session(run_id=run_id, profile=str(profile_path), headed=args.headed, actor="replay") as session:
        problems = validate_capability(capability, session.profile)
        if problems:
            raise CommandError("capability does not validate against the app profile: " + "; ".join(problems))
        cfg = session.settings
        credentials = (Credentials(cfg.cua_run_login, cfg.cua_run_password.get_secret_value())
                       if cfg.cua_run_password is not None else None)
        result = await replay(
            capability, session.profile, inputs, surface=session.surface, guard=session.guard, lease=session.lease,
            base_url=session.base_url, credentials=credentials, evidence=session.evidence, run_id=run_id,
        )
    return result, EXIT_CODES[result.status]


def cmd_replay(args: argparse.Namespace) -> int:
    try:
        result, code = asyncio.run(run_replay(args))
    except CommandError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE
    except (OSError, ValueError) as e:  # unreadable or invalid artifact files, a session that would not open
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_USAGE
    assert result is not None
    print(result.model_dump_json(indent=2) if args.json else render(result))
    return code


def cmd_compile(args: argparse.Namespace) -> int:
    from cua.compiler.compile import CompileError, compile_to_json  # lazy: replay itself never needs the compiler
    from cua.compiler.trace import RecordedTrace

    try:
        app, cap_name = _split_name(args.name)
        profile_path = Path(args.profile)
        if not profile_path.is_absolute() and not profile_path.exists():
            profile_path = REPO_ROOT / profile_path
        trace = RecordedTrace.model_validate_json(Path(args.trace).read_bytes())
        if trace.app != app:
            raise CommandError(f"--name is for app {app!r} but the trace is from app {trace.app!r}")
        text = compile_to_json(trace, load_profile(profile_path), name=cap_name,
                               discovered_by=args.discovered_by or trace.model, version=args.version,
                               title=args.title, description=args.description)
    except CommandError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE
    except CompileError as e:
        print("error: the trace does not compile:", file=sys.stderr)
        for problem in e.problems:
            print(f"  - {problem}", file=sys.stderr)
        return EXIT_FAILURE
    except (OSError, ValueError) as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_USAGE
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(text.encode("utf-8"))  # bytes: LF on every platform, the file the lock hash is computed from
    print(f"wrote {out}")
    return EXIT_SUCCESS


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("replay", help="run a compiled capability in a browser, with no model")
    p.add_argument("capability", help="<app>.<capability>, for example dolibarr.lookup_customer_outstanding")
    p.add_argument("--version", default=None, help="default: the highest version on disk")
    p.add_argument("--input", action="append", default=[], metavar="K=V", help="a capability input; repeat per input")
    p.add_argument("--headed", action="store_true", help="show the browser")
    p.add_argument("--json", action="store_true", help="print the RunResult as JSON")
    p.add_argument("--run-id", default=None, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_replay)

    c = subparsers.add_parser("compile", help="compile a recorded discovery trace into a capability draft")
    c.add_argument("trace", help="path to the recorded trace (cua.trace/v0 JSON)")
    c.add_argument("--profile", default="apps/dolibarr/profile.json")
    c.add_argument("--name", required=True, help="<app>.<capability>")
    c.add_argument("--out", required=True, help="where to write the draft")
    c.add_argument("--version", default="1.0.0")
    c.add_argument("--title", default=None)
    c.add_argument("--description", default=None)
    c.add_argument("--discovered-by", default=None, help="provenance label; default: the trace's model")
    c.set_defaults(func=cmd_compile)
