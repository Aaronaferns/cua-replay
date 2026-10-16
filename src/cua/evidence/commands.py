"""`cua make-evidence`: regenerate the replay evidence bundles from a capability's test suite.

    cua make-evidence <app>.<capability> [--version V] [--cases id,id] [--out evidence/]

For every case in `<capability>.tests.json` (or the ones named by `--cases`):

1. reset the target to its baseline (unless the case says `reset: false`);
2. open a contained, logged-in session (`cua.control.runtime.open_session`);
3. install the case's declared faults (sandbox policy only; see below);
4. run the replay;
5. write `result.json` and `ground_truth.json` into the run bundle through the evidence writer (which redacts), so the
   bundle's manifest covers them, then close the bundle in `runs/<run_id>/`;
6. copy the closed bundle to `<out>/replay_<case_id>/`.

`ground_truth.json` is the application's own REST view (a side channel the replay never uses): the oracle values the
suite names, and a hash of the whole REST state, before and after the run, plus the suite's expectation and what the
run reached. REST and the fault harness are imported lazily; replay itself never sees either.

Faults sidecar `<capability>@<version>.faults.json` (or `<capability>.faults.json`), beside the suite. The suite
schema (`cua.suite/v1`, owner B) has no fault field yet; when it gains one, this shape (a per-case list of fault
specs) is what to fold into `Case`:

    {
      "schema": "cua.faults/v1",
      "capability": "dolibarr.lookup_customer_outstanding",
      "cases": {
        "interstitial_recovered": [
          {"kind": "interstitial", "match": "/comm/card.php", "times": 1}
        ],
        "slow_within_timeout": [
          {"kind": "slow:250", "match": "/societe/**"}
        ]
      }
    }

`cases` maps a suite case id to a list of fault specs in the order the harness applies them. A spec is the harness
model (`cua.faults.harness`: `kind` of `slow` with `ms`, `app_error`, `interstitial`, `unknown_dialog`,
`lost_response_after_commit`, plus `match`, `methods`, `body_fields`, `times` and the kind's own fields), and `kind`
may use the plan's notation `slow:<ms>`. A case id that the suite does not have is an error. A case without an entry
runs without faults.

Exit codes: 0 every case produced a bundle that matches its expectation, 1 some case did not (or could not run),
2 the command itself could not run (bad arguments, files missing or invalid).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import shutil
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cua.safety.redact import safe_error_message

EXIT_OK, EXIT_MISMATCH, EXIT_USAGE = 0, 1, 2
FAULTS_SCHEMA = "cua.faults/v1"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class FaultsFile(_Strict):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True, validate_by_name=True, validate_by_alias=True
    )

    schema_: Literal["cua.faults/v1"] = Field(FAULTS_SCHEMA, alias="schema")
    capability: str
    cases: dict[str, list[dict[str, Any]]]


class EvidenceCommandError(Exception):
    """The command could not run; the message is for the person at the terminal and carries no values."""


def find_faults_file(name: str, version: str, folder: Path) -> Path | None:
    for candidate in (f"{name}@{version}.faults.json", f"{name}.faults.json"):
        if (folder / candidate).is_file():
            return folder / candidate
    return None


def load_faults(path: Path | None, capability_id: str, case_ids: set[str]) -> dict[str, list[dict[str, Any]]]:
    """The sidecar's `case id -> fault spec dicts`; empty when there is no sidecar."""
    if path is None:
        return {}
    try:
        data = FaultsFile.model_validate_json(path.read_bytes())
    except ValidationError as e:
        raise EvidenceCommandError(f"{path.name} is not a valid faults file: {safe_error_message(e)}") from None
    if data.capability != capability_id:
        raise EvidenceCommandError(f"{path.name} is for {data.capability!r}, not {capability_id!r}")
    unknown = sorted(set(data.cases) - case_ids)
    if unknown:
        raise EvidenceCommandError(f"{path.name} names cases the suite does not have: {', '.join(unknown)}")
    return {k: [dict(spec) for spec in v] for k, v in data.cases.items()}


def build_faults(specs: list[dict[str, Any]]) -> list[Any]:
    """Harness specs from sidecar dicts (lazy: the fault harness is imported only when a case declares faults)."""
    from cua.faults.harness import parse_fault

    out = []
    for spec in specs:
        fields = dict(spec)
        kind, match = fields.pop("kind", None), fields.pop("match", None)
        if not isinstance(kind, str) or not isinstance(match, str):
            raise EvidenceCommandError("a fault needs a string 'kind' and a string 'match'")
        try:
            out.append(parse_fault(kind, match=match, **fields))
        except (ValueError, ValidationError) as e:
            raise EvidenceCommandError(f"invalid fault {kind!r}: {safe_error_message(e)}") from None
    return out


# ------------------------------------------------------------------------------------------------------ seams


@dataclass
class Hooks:
    """What the command needs from the outside world; tests replace them with fakes.

    reset()              restore the target to its baseline (blocking)
    rest_view(case)      the application's REST view for a case (blocking): {"state_sha256": str, "oracles": {...}}
    open_session(**kw)   `cua.control.runtime.open_session`
    replay(...)          `cua.replay.interpreter.replay`
    captcha(on)          switch the target's login CAPTCHA on/off (blocking; `--handoff` only)
    """

    reset: Callable[[], None]
    rest_view: Callable[[Any], dict[str, Any]]
    open_session: Callable[..., Any]
    replay: Callable[..., Awaitable[Any]]
    captcha: Callable[[bool], None] | None = None


def default_hooks() -> Hooks:
    from cua.control.runtime import open_session
    from cua.replay.interpreter import replay

    return Hooks(reset=_reset_target, rest_view=_rest_view, open_session=open_session, replay=replay, captcha=_captcha)


def _captcha(on: bool) -> None:
    _target_tools()
    import dolictl

    dolictl.run_script("captcha.sh", "on" if on else "off")


def _target_tools() -> None:
    from cua.config import REPO_ROOT

    path = str(REPO_ROOT / "target" / "dolibarr")
    if path not in sys.path:
        sys.path.insert(0, path)


def _expire_session() -> None:
    _target_tools()
    from livetarget import expire_session

    expire_session()


def _reset_target() -> None:
    """Baseline restore. `reset.sh` exits 4 when the restore finished but overran its 15 s budget on a loaded host; the
    restore happened, so only the fixture check is repeated."""
    _target_tools()
    import dolictl
    from livetarget import reset_to_baseline

    try:
        reset_to_baseline()
    except dolictl.CtlError as e:
        if "budget" not in str(e):
            raise
        import seed
        from dolirest import Rest, customer_by_code, outstanding_of

        from cua.config import settings

        admin = Rest.admin(settings())
        for c in seed.load_fixtures().customers:
            if outstanding_of(admin, customer_by_code(admin, c.code)["id"]) != c.outstanding:
                raise


def _rest_view(case: Any) -> dict[str, Any]:
    _target_tools()
    from dolirest import Rest, customer_by_code, outstanding_of, state_snapshot

    from cua.config import settings

    admin = Rest.admin(settings())
    state = state_snapshot(admin)
    digest = hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    oracles: dict[str, str | None] = {}
    for truth in case.expect.ground_truth:
        if truth.oracle != "dolibarr.customer_outstanding":
            raise EvidenceCommandError(f"unknown oracle {truth.oracle!r}")
        customer = customer_by_code(admin, case.inputs[truth.input])
        oracles[truth.output] = None if customer is None else str(outstanding_of(admin, customer["id"]))
    view: dict[str, Any] = {"state_sha256": digest, "oracles": oracles}
    if "invoice_ref" in case.inputs:  # a commit case: the payments the invoice really has (count and amounts)
        view["payments"] = _invoice_payments(admin, case.inputs["invoice_ref"])
    return view


def _invoice_payments(admin: Any, invoice_ref: str) -> dict[str, Any] | None:
    from dolirest import sql_eq

    status, invoice = admin.call("GET", f"/invoices/ref/{invoice_ref}")
    if status != 200:
        found = admin.listing("/invoices", sqlfilters=sql_eq("t.ref_client", invoice_ref))
        invoice = found[0] if found else None
    if invoice is None:
        return None
    status, rows = admin.call("GET", f"/invoices/{invoice['id']}/payments")  # 404 "No payment" = none
    amounts = sorted(str(Decimal(str(p["amount"]))) for p in rows) if status == 200 else []
    return {"invoice_id": str(invoice["id"]), "count": len(amounts), "amounts": amounts}


# ------------------------------------------------------------------------------------------------- the run


@dataclass
class CaseOutcome:
    case_id: str
    bundle: Path | None  # <out>/replay_<case_id>
    status: str | None
    outcome: str | None
    mismatches: list[str]
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and not self.mismatches


async def make_case_evidence(
    case: Any,
    capability: Any,
    *,
    fault_specs: list[dict[str, Any]],
    hooks: Hooks,
    out: Path,
    stamp: str,
    runs_root: Path | None,
    profile_path: Path,
    handoff: bool = False,
    console: bool = False,
    name: str | None = None,
    policy: str | None = None,
) -> CaseOutcome:
    from cua.artifact.validate import validate_capability
    from cua.replay.interpreter import Credentials
    from cua.replay.suite import compare

    run_id = f"evid-{stamp}-{case.id}"[:100]
    if policy:  # a policy that denies a route the capability needs: the run must stop at the static preflight
        expect = case.expect.model_copy(update={"status": "failure", "outcome": "POLICY_DENIED", "outputs": {},
                                                "ground_truth": [], "payments": case.expect.payments and
                                                case.expect.payments.model_copy(update={"amounts": []})})
        case = case.model_copy(update={"expect": expect})
    fault_specs = [*case.faults, *fault_specs]  # the suite case's own faults, then the sidecar's
    faults = build_faults(fault_specs)  # fail on a bad sidecar before touching the target
    console = console or handoff  # a live console + headed browser; --handoff adds the login CAPTCHA
    if case.reset:
        await asyncio.to_thread(hooks.reset)
    before = await asyncio.to_thread(hooks.rest_view, case)
    if handoff:  # the human-only login gate: replay logs in itself, meets the CAPTCHA and escalates to the console
        assert hooks.captcha is not None
        await asyncio.to_thread(hooks.captcha, True)

    result = None
    installation = None
    after: dict[str, Any] = {}
    mismatches: list[str] = []
    session_kw: dict[str, Any] = {"login": False} if case.run_as or handoff else {}
    if console:
        session_kw["headed"] = True
    if policy:
        session_kw |= {"policy": policy, "login": False}  # not even the login page is opened
    elif capability.effect == "commit":  # the same policy validate uses: the read-only sandbox denies the commit route
        session_kw["policy"] = f"{capability.app}-sandbox-commit"
    inputs = {k: v.replace("{run_id}", run_id) for k, v in case.inputs.items()}  # a fresh request_id, as in validate
    async with hooks.open_session(
        run_id=run_id, profile=str(profile_path), actor="replay", evidence_root=runs_root, **session_kw
    ) as session:
        problems = validate_capability(capability, session.profile)
        if problems:
            raise EvidenceCommandError("capability does not validate against the app profile: " + "; ".join(problems))
        writer = session.evidence
        try:
            if faults:
                from cua.faults.harness import install_faults

                def on_fault(event: Any) -> None:
                    writer.emit("fault", actor="system", detail=event.model_dump(mode="json"))

                context = session.surface.page.context
                installation = await install_faults(context, session.policy, faults, on_fault=on_fault)
            cfg = session.settings
            credentials = (
                Credentials(cfg.cua_run_login, cfg.cua_run_password.get_secret_value())
                if cfg.cua_run_password is not None
                else None
            )
            extra: dict[str, Any] = {}
            if case.run_as == "noperm":
                from cua.replay.commands import noperm_login

                credentials = Credentials(noperm_login(), cfg.cua_noperm_password.get_secret_value())
            if case.expire_session_before:
                expired = False

                async def before_step(step: Any) -> None:
                    nonlocal expired
                    if step.id == case.expire_session_before and not expired:
                        expired = True
                        await asyncio.to_thread(_expire_session)

                extra["before_step"] = before_step
            async with contextlib.AsyncExitStack() as stack:
                if capability.effect == "commit":
                    from cua.replay.commit_ledger import CommitLedger

                    extra["ledger"] = stack.enter_context(contextlib.closing(CommitLedger((runs_root or Path("runs")) / "commit_ledger.sqlite")))
                if console:
                    from cua.control.commands import live_console

                    extra["hub"] = hub = await stack.enter_async_context(live_console(session))
                    print(f"console: {hub.console_url}  (claim, then resume / give the verdict)", flush=True)
                result = await hooks.replay(
                    capability, session.profile, inputs, surface=session.surface, guard=session.guard,
                    lease=session.lease, base_url=session.base_url, credentials=credentials, evidence=writer,
                    run_id=run_id, **extra,
                )
        finally:
            if installation is not None:
                await installation.uninstall()
            if handoff:
                await asyncio.to_thread(hooks.captcha, False)
        after = await asyncio.to_thread(hooks.rest_view, case)
        truth = {k: v for k, v in after["oracles"].items() if v is not None}
        paid = after.get("payments")
        payments = [f"{Decimal(a):.2f}" for a in paid["amounts"]] if paid else None  # the suite's format, as validate
        mismatches = compare(case, result, truth, list(capability.contract.outputs), payments)
        writer.json("result.json", result.model_dump(mode="json"))
        writer.json(
            "ground_truth.json",
            {
                "schema": "cua.ground_truth/v1",
                "source": "Dolibarr REST (admin key): a side channel the replay never uses",
                "capability": capability.id,
                "case": case.id,
                "inputs": inputs,
                "faults": [{k: v for k, v in s.items()} for s in fault_specs],
                "before": before,
                "after": after,
                "rest_state_unchanged": before["state_sha256"] == after["state_sha256"],
                **(
                    {"new_payments": after["payments"]["count"] - before["payments"]["count"]}
                    if before.get("payments") and after.get("payments")
                    else {}
                ),
                "expected": {"status": case.expect.status, "outcome": case.expect.outcome},
                "actual": {"status": result.status, "outcome": result.outcome, "outputs": dict(result.outputs)},
                "mismatches": mismatches,
            },
        )
        run_dir = writer.run_dir
    # the session is closed: the manifest now lists every file, result.json and ground_truth.json included
    target = out / (name or ("replay_captcha_handoff" if handoff else f"replay_{case.id}"))
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(run_dir, target)
    return CaseOutcome(case.id, target, result.status, result.outcome, mismatches)


async def run_make_evidence(args: argparse.Namespace, hooks: Hooks | None = None) -> tuple[list[CaseOutcome], int]:
    from cua.artifact.lock import load_capability
    from cua.artifact.suite import Suite, suite_problems
    from cua.config import REPO_ROOT
    from cua.replay.commands import CommandError, find_capability_file, find_suite_file, profile_path_of

    try:
        path, _draft = find_capability_file(
            args.capability, args.version, Path(args.capabilities_dir) if args.capabilities_dir else None
        )
        capability = load_capability(path)
        suite = Suite.model_validate_json(find_suite_file(capability, path.parent).read_bytes())
    except CommandError as e:
        raise EvidenceCommandError(str(e)) from None
    except ValidationError as e:
        raise EvidenceCommandError(f"a capability or suite file is invalid: {safe_error_message(e)}") from None
    if problems := suite_problems(suite, capability):
        raise EvidenceCommandError("; ".join(problems))
    ids = [c.id for c in suite.cases]
    wanted = [c for c in args.cases.split(",") if c] if args.cases else ids
    if unknown := sorted(set(wanted) - set(ids)):
        raise EvidenceCommandError(f"no such case in the suite: {', '.join(unknown)}")
    if (args.handoff or args.console) and len(wanted) != 1:
        raise EvidenceCommandError("--handoff and --console take exactly one case (--cases <id>)")
    if args.name and (len(wanted) != 1 or "/" in args.name or "\\" in args.name or args.name.startswith(".")):
        raise EvidenceCommandError("--name is one folder name and needs exactly one case")
    fault_map = load_faults(find_faults_file(capability.name, capability.version, path.parent), capability.id, set(ids))
    out = Path(args.out)
    if not out.is_absolute():
        out = REPO_ROOT / out
    runs_root = Path(args.runs_root) if args.runs_root else None
    hooks = hooks or default_hooks()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    outcomes: list[CaseOutcome] = []
    for case in suite.cases:
        if case.id not in wanted:
            continue
        try:
            outcome = await make_case_evidence(
                case, capability, fault_specs=fault_map.get(case.id, []), hooks=hooks, out=out, stamp=stamp,
                runs_root=runs_root, profile_path=profile_path_of(capability.app), handoff=args.handoff,
                console=args.console, name=args.name,
                policy=getattr(args, "policy", None),
            )
        except Exception as e:  # noqa: BLE001 - one broken case must not stop the others
            outcome = CaseOutcome(case.id, None, None, None, [], error=safe_error_message(e))
        outcomes.append(outcome)
    return outcomes, EXIT_OK if all(o.ok for o in outcomes) else EXIT_MISMATCH


def cmd_make_evidence(args: argparse.Namespace) -> int:
    try:
        outcomes, code = asyncio.run(run_make_evidence(args))
    except EvidenceCommandError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE
    except (OSError, ValueError) as e:
        print(f"error: {safe_error_message(e)}", file=sys.stderr)
        return EXIT_USAGE
    for o in outcomes:
        if o.error is not None:
            print(f"ERROR  {o.case_id}: {o.error}")
            continue
        print(f"{'PASS' if o.ok else 'FAIL'}  {o.case_id}  {o.status}/{o.outcome}  -> {o.bundle}")
        for m in o.mismatches:
            print(f"      - {m}")
    return code


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "make-evidence", help="regenerate replay evidence bundles from a capability's test suite (resets the target)"
    )
    p.add_argument("capability", help="<app>.<capability>")
    p.add_argument("--version", default=None, help="default: the highest version on disk")
    p.add_argument("--cases", default=None, help="comma-separated case ids; default: every case in the suite")
    p.add_argument("--out", default="evidence/", help="where the replay_<case_id>/ folders go")
    p.add_argument("--handoff", action="store_true",
                   help="human handoff bundle: CAPTCHA on, live console, headed; one case -> replay_captcha_handoff/")
    p.add_argument("--console", action="store_true",
                   help="live console + headed browser, no CAPTCHA: a human gives the resolve_commit verdict on "
                        "COMMIT_UNKNOWN (the escalation waits for it, bounded by the 15 min SLA); one case")
    p.add_argument("--name", default=None, help="bundle folder name instead of replay_<case_id> (one case)")
    p.add_argument("--policy", default=None,
                   help="run each case under this policy (for example dolibarr-sandbox-deny-payment) and expect "
                        "failure/POLICY_DENIED at the static preflight, with REST state unchanged")
    p.add_argument("--runs-root", default=None, help=argparse.SUPPRESS)  # tests: where run bundles are written
    p.add_argument("--capabilities-dir", default=None, help=argparse.SUPPRESS)  # tests: a folder of <app>/ folders
    p.set_defaults(func=cmd_make_evidence)

    lint = subparsers.add_parser("lint", help="scan evidence/ for secrets, SSNs, cookies and unmasked e-mails")
    lint.add_argument("--root", default="evidence/")
    from cua.safety.lint import cmd_lint

    lint.set_defaults(func=cmd_lint)

    demo = subparsers.add_parser("demo", help="demo helpers")
    demo_sub = demo.add_subparsers(dest="demo_command", required=True)
    up = demo_sub.add_parser("up", help="start the target, wait for health, reset it, print the URL")
    up.add_argument("--timeout", type=int, default=600, help="seconds to wait for health")
    from cua.evidence.demo import cmd_demo_up

    up.set_defaults(func=cmd_demo_up)
