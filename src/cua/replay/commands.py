"""`cua replay`, `cua compile`, `cua approve`, `cua capabilities` and `cua validate`.

    cua replay <app>.<capability> [--version 1.0.0] --input k=v ... [--headed] [--json] [--allow-draft]
    cua compile <trace.json> [--profile apps/dolibarr/profile.json] --name <app.capability> --out <path>
    cua approve <app>.<capability> --version V --approver <id> --run <validation_run_id> [--run ...]
    cua capabilities [--json]
    cua validate <app>.<capability> [--version V] [--repeat N] [--json]
    cua review <app>.<capability> [--version V] [--out path]

Exit codes (replay): 0 success, 1 failure, 2 the command itself could not run (bad arguments, artifact not found or
invalid, session could not open, or the capability is not approved and `--allow-draft` was not given), 3 business
outcome, 4 indeterminate. Replay runs only a capability whose approval sidecar matches its current lock hash; a
draft file, a missing sidecar or a stale one is refused unless `--allow-draft`, which runs it with a warning.

Exit codes (approve): 0 written, 1 refused (self-approval, or validation runs missing, failed or for another lock
hash), 2 the command could not run. Exit codes (validate): 0 every case matched its expectations, 1 some case did
not, 2 the command could not run. `capabilities` exits 0.

This is CLI wiring, not replay logic: it is the one module in this package allowed to open a browser session
(through `cua.control.runtime`, imported lazily). It still imports no model code. `compile` imports the compiler
lazily, inside the command.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cua.artifact.approval import (
    Approval,
    ApprovalState,
    SelfApprovalError,
    approval_state,
    approve,
    dumps_approval,
    load_approval,
    sidecar_path,
)
from cua.artifact.lock import load_capability, load_profile, lock_hash
from cua.artifact.schema import Capability, Profile
from cua.artifact.suite import Case, Suite, suite_problems
from cua.artifact.validate import validate_capability
from cua.replay.suite import CaseRun, Summary, check_validation_runs, compare
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


def approvals_dir() -> Path:
    return REPO_ROOT / "approvals"


def validation_dir() -> Path:
    """Where `cua validate` writes `<run_id>/summary.json` (runtime output, never committed)."""
    return REPO_ROOT / "runs" / "validation"


def profile_path_of(app: str) -> Path:
    return REPO_ROOT / "apps" / app / "profile.json"


def approval_of(capability: Capability, profile: Profile, *, draft: bool, root: Path | None = None) -> tuple[ApprovalState, Approval | None]:
    """A draft file is never approved (promote it to `<cap>@<version>.json`, then approve). A sidecar that cannot be
    read counts as stale: something is there, and it does not vouch for this."""
    try:
        approval = load_approval(sidecar_path(root or approvals_dir(), capability))
    except (OSError, ValueError):
        return "stale", None
    if draft:
        return "unapproved", approval
    return approval_state(capability, profile, approval), approval


def _noperm_login() -> str:
    _target_tools()
    import seed

    return seed.load_fixtures().noperm_login


async def _execute(
    capability: Capability, inputs: dict[str, str], *, run_id: str, headed: bool = False, case: Case | None = None,
    handoff: bool = False,
) -> RunResult:
    """One replay in a fresh session (shared by `replay` and `validate`). A suite `case` may run as the no-rights
    user, install faults on the browser context, and expire the session server-side before a step: all of it is done
    here, around replay, which never imports the fault harness."""
    profile_path = profile_path_of(capability.app)
    run_as = case.run_as if case else None
    async with _open_session(run_id=run_id, profile=str(profile_path), headed=headed, actor="replay",
                             login=run_as is None and not handoff) as session:  # handoff: replay logs in, so a CAPTCHA escalates
        problems = validate_capability(capability, session.profile)
        if problems:
            raise CommandError("capability does not validate against the app profile: " + "; ".join(problems))
        cfg = session.settings
        if run_as == "noperm":
            credentials = Credentials(_noperm_login(), cfg.cua_noperm_password.get_secret_value())
        else:
            credentials = (Credentials(cfg.cua_run_login, cfg.cua_run_password.get_secret_value())
                           if cfg.cua_run_password is not None else None)
        installation = None
        if case and case.faults:
            from cua.evidence.commands import build_faults
            from cua.faults.harness import install_faults

            def on_fault(event: Any) -> None:
                if session.evidence is not None:
                    session.evidence.emit("fault", actor="system", detail=event.model_dump(mode="json"))

            installation = await install_faults(session.surface.page.context, session.policy,
                                                build_faults(case.faults), on_fault=on_fault)
        expired = False

        async def before_step(step: Any) -> None:
            nonlocal expired
            if case and case.expire_session_before == step.id and not expired:
                expired = True
                _target_tools()
                from livetarget import expire_session

                await asyncio.to_thread(expire_session)

        try:
            async with contextlib.AsyncExitStack() as stack:
                hub = None
                if handoff and getattr(session, "lease_manager", None) is not None:  # interactive replay: a HUMAN_ONLY_LOGIN blocks here on the live console
                    from cua.control.commands import live_console

                    hub = await stack.enter_async_context(live_console(session))
                    print(f"console: {hub.console_url}  (loopback only)", file=sys.stderr, flush=True)
                return await replay(
                    capability, session.profile, inputs, surface=session.surface, guard=session.guard,
                    lease=session.lease, base_url=session.base_url, credentials=credentials, evidence=session.evidence,
                    run_id=run_id, before_step=before_step if case and case.expire_session_before else None, hub=hub,
                )
        finally:
            if installation is not None:
                await installation.uninstall()


async def run_replay(args: argparse.Namespace) -> tuple[RunResult | None, int]:
    path, draft = find_capability_file(args.capability, args.version, Path(args.capabilities_dir) if args.capabilities_dir else None)
    capability: Capability = load_capability(path)
    profile = load_profile(profile_path_of(capability.app))
    state, _ = approval_of(capability, profile, draft=draft,
                           root=Path(args.approvals_dir) if args.approvals_dir else None)
    if state != "approved":
        what = "a draft" if draft else f"{state}"
        if not args.allow_draft:
            raise CommandError(
                f"{path.name} is {what} (approval: {state}); replay runs approved capabilities only. "
                "Review and approve it (cua approve), or pass --allow-draft to run it anyway")
        print(f"warning: running {path.name} without a valid approval ({what}; --allow-draft)", file=sys.stderr)
    inputs = parse_inputs(args.input)
    run_id = args.run_id or f"replay-{time.strftime('%Y%m%d-%H%M%S')}-{capability.id.split('.')[-1][:24]}"
    result = await _execute(capability, inputs, run_id=run_id, headed=args.headed, handoff=True)
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


def cmd_review(args: argparse.Namespace) -> int:
    from cua.compiler.review_card import render_review_card  # lazy: replay itself never needs the compiler

    try:
        path, _ = find_capability_file(args.capability, args.version)
        capability = load_capability(path)
        text = render_review_card(capability, load_profile(profile_path_of(capability.app)))
        out = Path(args.out) if args.out else path.with_name(f"{capability.name}@{capability.version}.review.md")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(text.encode("utf-8"))  # bytes: LF on every platform
    except CommandError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE
    except (OSError, ValueError) as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_USAGE
    print(f"wrote {out}")
    return EXIT_SUCCESS


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


def cmd_approve(args: argparse.Namespace) -> int:
    try:
        path, draft = find_capability_file(args.capability, args.version)
        if draft:
            raise CommandError(f"{path.name} is a draft: review it and promote it to a final "
                               f"{path.name.replace('.draft', '')} before approving")
        capability = load_capability(path)
        profile = load_profile(profile_path_of(capability.app))
        if problems := validate_capability(capability, profile):
            raise CommandError("capability does not validate against the app profile: " + "; ".join(problems))
        run_ids = list(dict.fromkeys(args.run))
        try:
            approval = approve(capability, profile, approver=args.approver, at=datetime.now(UTC),
                               validation_run_ids=run_ids)
        except SelfApprovalError as e:
            print(f"refused: {e}", file=sys.stderr)
            return EXIT_FAILURE
        problems = check_validation_runs(validation_dir(), run_ids, capability=capability.id,
                                         version=capability.version, lock_hash=approval.lock_hash)
        if problems:
            print("refused: the cited validation runs do not support this approval:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            return EXIT_FAILURE
        out = sidecar_path(approvals_dir(), capability)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(dumps_approval(approval).encode("utf-8"))
    except CommandError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE
    except (OSError, ValueError) as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_USAGE
    print(f"approved {capability.id}@{capability.version} (lock {approval.lock_hash[:12]}) -> {out}")
    return EXIT_SUCCESS


def list_capabilities(root: Path | None = None, approvals: Path | None = None) -> list[dict[str, Any]]:
    """Every capability file under `capabilities/<app>/`: id, version, effect, approval state, lock hash."""
    base = root or capabilities_dir()
    rows: list[dict[str, Any]] = []
    for app_dir in sorted(p for p in base.glob("*") if p.is_dir()) if base.is_dir() else []:
        for path in sorted(app_dir.glob("*.json")):
            m = _VERSIONED.fullmatch(path.name)
            if m is None:
                continue
            draft = m.group("draft") is not None
            row: dict[str, Any] = {"id": f"{app_dir.name}.{m.group('cap')}", "version": m.group("ver"),
                                   "file": f"{app_dir.name}/{path.name}", "draft": draft}
            try:
                capability = load_capability(path)
                profile = load_profile(profile_path_of(capability.app))
                state, _ = approval_of(capability, profile, draft=draft, root=approvals)
                row |= {"effect": capability.effect, "approval": state, "lock_hash": lock_hash(capability, profile)}
            except (OSError, ValueError) as e:
                row |= {"effect": None, "approval": "unapproved", "lock_hash": None, "error": type(e).__name__}
            rows.append(row)
    return sorted(rows, key=lambda r: (r["id"], _version_key(r["version"]), r["draft"]))


def cmd_capabilities(args: argparse.Namespace) -> int:
    rows = list_capabilities(approvals=Path(args.approvals_dir) if args.approvals_dir else None)
    if args.json:
        print(json.dumps(rows, indent=2))
        return EXIT_SUCCESS
    if not rows:
        print("no capabilities")
    for r in rows:
        tag = " (draft)" if r["draft"] else ""
        print(f"{r['id']}@{r['version']}{tag}  effect={r['effect']}  {r['approval']}  lock={(r['lock_hash'] or '-')[:12]}")
    return EXIT_SUCCESS


# --- validate: fixtures path only (reset the target, consult an oracle), never replay itself -----------------------


def _target_tools() -> None:
    """Make the fixtures helpers (`target/dolibarr`) importable. Imported lazily, inside the commands that need them."""
    path = str(REPO_ROOT / "target" / "dolibarr")
    if path not in sys.path:
        sys.path.insert(0, path)


def _reset_target() -> None:
    _target_tools()
    from livetarget import reset_to_baseline

    reset_to_baseline()  # a slow restore only warns; the fixture check inside is the real test


def _oracle(name: str, value: str) -> str:
    if name == "dolibarr.customer_outstanding":
        _target_tools()
        from dolirest import Rest, customer_by_code, outstanding_of

        from cua.config import settings

        admin = Rest.admin(settings())
        customer = customer_by_code(admin, value)
        if customer is None:
            raise CommandError("the ground-truth oracle found no such customer")
        return str(outstanding_of(admin, customer["id"]))
    raise CommandError(f"unknown oracle {name!r}")


def find_suite_file(capability: Capability, folder: Path) -> Path:
    for name in (f"{capability.name}@{capability.version}.tests.json", f"{capability.name}.tests.json"):
        if (folder / name).is_file():
            return folder / name
    raise CommandError(f"no suite {capability.name}.tests.json under {folder}")


async def run_validate(args: argparse.Namespace) -> tuple[Summary, int]:
    path, _ = find_capability_file(args.capability, args.version)
    capability = load_capability(path)
    profile = load_profile(profile_path_of(capability.app))
    suite = Suite.model_validate_json(find_suite_file(capability, path.parent).read_bytes())
    if problems := validate_capability(capability, profile) + suite_problems(suite, capability):
        raise CommandError("; ".join(problems))
    if args.repeat < 1:
        raise CommandError("--repeat must be at least 1")
    run_id = args.run_id or f"val-{time.strftime('%Y%m%d-%H%M%S')}-{capability.name[:24]}"
    started = datetime.now(UTC)
    runs: list[CaseRun] = []
    for repeat in range(1, args.repeat + 1):
        for case in suite.cases:
            case_run_id = f"{run_id}-{case.id}-{repeat}"
            began = time.monotonic()
            status = outcome = None
            try:
                if case.reset:
                    await asyncio.to_thread(_reset_target)
                result = await _execute(capability, dict(case.inputs), run_id=case_run_id, case=case)
                status, outcome = result.status, result.outcome
                truth = {g.output: await asyncio.to_thread(_oracle, g.oracle, case.inputs[g.input])
                         for g in case.expect.ground_truth}
                mismatches = compare(case, result, truth, list(capability.contract.outputs))
            except Exception as e:  # a case that cannot be run is a failed case, not a crashed validation
                mismatches = [f"the case could not be run: {type(e).__name__}"]
            runs.append(CaseRun(case_id=case.id, repeat=repeat, run_id=case_run_id, passed=not mismatches,
                                status=status, outcome=outcome, mismatches=mismatches,
                                duration_ms=int((time.monotonic() - began) * 1000)))
    summary = Summary(run_id=run_id, capability=capability.id, version=capability.version,
                      lock_hash=lock_hash(capability, profile), repeat=args.repeat, started_at=started,
                      passed=all(r.passed for r in runs), cases=runs)
    out = validation_dir() / run_id / "summary.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes((json.dumps(json.loads(summary.model_dump_json(by_alias=True)), indent=2) + "\n").encode("utf-8"))
    return summary, EXIT_SUCCESS if summary.passed else EXIT_FAILURE


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        summary, code = asyncio.run(run_validate(args))
    except CommandError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE
    except (OSError, ValueError) as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_USAGE
    if args.json:
        print(summary.model_dump_json(by_alias=True, indent=2))
    else:
        for r in summary.cases:
            print(f"{'PASS' if r.passed else 'FAIL'}  {r.case_id} #{r.repeat}  {r.status}/{r.outcome}")
            for m in r.mismatches:
                print(f"      - {m}")
        n = sum(r.passed for r in summary.cases)
        print(f"{n}/{len(summary.cases)} passed; run {summary.run_id}; lock {summary.lock_hash[:12]}")
    return code


def register(subparsers: argparse._SubParsersAction) -> None:
    a = subparsers.add_parser("approve", help="record a second person's approval of a capability's current lock hash")
    a.add_argument("capability", help="<app>.<capability>")
    a.add_argument("--version", required=True)
    a.add_argument("--approver", required=True, help="who approves; must not be the person who ran the discovery")
    a.add_argument("--run", action="append", required=True, metavar="VALIDATION_RUN_ID",
                   help="a passing `cua validate` run for this lock hash; repeat for several")
    a.set_defaults(func=cmd_approve)

    ls = subparsers.add_parser("capabilities", help="list capabilities with version, effect, approval state, lock hash")
    ls.add_argument("--json", action="store_true")
    ls.add_argument("--approvals-dir", default=None, help=argparse.SUPPRESS)
    ls.set_defaults(func=cmd_capabilities)

    v = subparsers.add_parser("validate", help="run a capability's test suite, resetting the target per case")
    v.add_argument("capability", help="<app>.<capability>")
    v.add_argument("--version", default=None)
    v.add_argument("--repeat", type=int, default=1, help="run the whole suite N times")
    v.add_argument("--json", action="store_true")
    v.add_argument("--run-id", default=None, help=argparse.SUPPRESS)
    v.set_defaults(func=cmd_validate)

    p = subparsers.add_parser("replay", help="run a compiled capability in a browser, with no model")
    p.add_argument("--allow-draft", action="store_true",
                   help="run a draft, unapproved or stale capability anyway (with a warning)")
    p.add_argument("--approvals-dir", default=None, help=argparse.SUPPRESS)
    p.add_argument("capability", help="<app>.<capability>, for example dolibarr.lookup_customer_outstanding")
    p.add_argument("--version", default=None, help="default: the highest version on disk")
    p.add_argument("--input", action="append", default=[], metavar="K=V", help="a capability input; repeat per input")
    p.add_argument("--headed", action="store_true", help="show the browser")
    p.add_argument("--json", action="store_true", help="print the RunResult as JSON")
    p.add_argument("--run-id", default=None, help=argparse.SUPPRESS)
    p.add_argument("--capabilities-dir", default=None, help=argparse.SUPPRESS)  # tests: a folder of <app>/ folders
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

    r = subparsers.add_parser("review", help="render the review card (<capability>@<version>.review.md)")
    r.add_argument("capability", help="<app>.<capability>")
    r.add_argument("--version", default=None)
    r.add_argument("--out", default=None, help="default: beside the capability file")
    r.set_defaults(func=cmd_review)
