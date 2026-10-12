"""`cua discover`: let the model work a goal on the live application, record it, and compile a draft.

    cua discover lookup_customer_outstanding \\
        --goal "Find the outstanding amount of customer {{customer_code}}" --input customer_code=CU2601-00901

Writes `runs/<run_id>/` (manifest, events with rationales, redacted transcript and trace, masked screenshots, the
compiled draft). Needs ANTHROPIC_API_KEY, a running target and the browser; `--transcript` replays a recorded
transcript with no model instead.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from cua.artifact.schema import FieldSpec

# What each known capability takes and returns (`--output name:type` declares outputs for any other capability).
KNOWN: dict[str, tuple[dict[str, FieldSpec], dict[str, FieldSpec]]] = {
    "lookup_customer_outstanding": (
        {"customer_code": FieldSpec(type="string", pattern=r"CU\d{4}-\d{4,5}", sensitivity="pii",
                                    description="the customer code")},
        {"outstanding_amount": FieldSpec(type="decimal", description="the current outstanding bill amount")},
    ),
}
_TYPES = ("string", "decimal", "integer", "date")


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("discover", help="run the model on a goal, record the run and compile a draft")
    p.add_argument("capability", help="capability name, e.g. lookup_customer_outstanding")
    p.add_argument("--goal", required=True, help="the goal; write inputs as {{name}} placeholders")
    p.add_argument("--input", action="append", default=[], metavar="NAME=VALUE", help="an input value (repeatable)")
    p.add_argument("--output", action="append", default=[], metavar="NAME:TYPE",
                   help="a declared output (known capabilities have theirs)")
    p.add_argument("--headed", action="store_true", help="show the browser")
    p.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--screenshots", action="store_true", help="send a screenshot with each observation (default: text only)")
    p.add_argument("--run-id", help="run directory name (default: discovery_<capability>_<UTC time>)")
    p.add_argument("--max-steps", type=int, default=30)
    p.add_argument("--wall-clock", type=float, default=420.0, help="seconds")
    p.add_argument("--discovered-by", help="who discovered it (default: the OS user)")
    p.add_argument("--policy", default="dolibarr-sandbox")
    p.add_argument("--profile", default="apps/dolibarr/profile.json")
    p.add_argument("--evidence-root", type=Path, help="where run directories go (default: runs/)")
    p.add_argument("--transcript", type=Path, help="replay a recorded transcript.json instead of calling the model")
    p.set_defaults(func=run)


def _specs(args: argparse.Namespace) -> tuple[dict[str, str], dict[str, FieldSpec], dict[str, FieldSpec]]:
    known_inputs, known_outputs = KNOWN.get(args.capability, ({}, {}))
    values: dict[str, str] = {}
    for item in args.input:
        name, sep, value = item.partition("=")
        if not sep or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise SystemExit(f"--input must look like name=value (got {name!r})")
        values[name] = value
    inputs = {n: known_inputs.get(n, FieldSpec(type="string")) for n in values}
    for name, value in values.items():
        pattern = inputs[name].pattern
        if pattern and not re.fullmatch(pattern, value):
            raise SystemExit(f"input {name} does not match its pattern {pattern}")
    outputs = dict(known_outputs)
    for item in args.output:
        name, _, typ = item.partition(":")
        if typ not in _TYPES:
            raise SystemExit(f"--output must look like name:type with type one of {', '.join(_TYPES)}")
        outputs[name] = FieldSpec(type=typ)  # type: ignore[arg-type]
    if not outputs:
        raise SystemExit("this capability declares no outputs: pass --output name:type")
    for token in re.findall(r"\{\{([a-z][a-z0-9_]*)\}\}", args.goal):
        if token not in values:
            raise SystemExit(f"the goal mentions {{{{{token}}}}} but no --input {token}=... was given")
    return values, inputs, outputs


async def _run(args: argparse.Namespace) -> int:
    from cua.agent.discovery import discover
    from cua.config import settings
    from cua.control.runtime import open_session

    values, inputs, outputs = _specs(args)
    cfg = settings()
    if args.transcript:
        from cua.agent.scripted_adapter import ScriptedAdapter

        adapter = ScriptedAdapter.from_transcript(args.transcript)
    else:
        from cua.agent.anthropic_adapter import AnthropicAdapter

        adapter = AnthropicAdapter(cfg.anthropic_api_key, effort=args.effort, screenshots=args.screenshots)
    run_id = args.run_id or f"discovery_{args.capability}_{datetime.now(UTC):%Y%m%dT%H%M%SZ}"

    async with open_session(run_id=run_id, policy=args.policy, profile=args.profile, headed=args.headed,
                            evidence_root=args.evidence_root, actor="agent", config=cfg) as session:
        result = await discover(session, adapter, capability=args.capability, goal=args.goal, input_values=values,
                                input_specs=inputs, output_specs=outputs, discovered_by=args.discovered_by,
                                max_steps=args.max_steps, wall_clock_s=args.wall_clock)

    loop = result.loop
    total = {k: sum(getattr(u, k) for u in loop.usage) for k in
             ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")}
    print(f"run:       {result.run_dir}")
    print(f"outcome:   {loop.outcome}" + (f" ({loop.stop_reason})" if loop.stop_reason else ""))
    print(f"steps:     {loop.steps}")
    for name, text in loop.outputs.items():
        print(f"output:    {name} = {text}")
    print(f"tokens:    {total}")
    print(f"draft:     {'compiled: ' + str(result.draft_path) if result.compiled else 'NOT compiled'}")
    for problem in result.compile_problems:
        print(f"  - {problem}")
    return 0 if loop.outcome == "done" and result.compiled else 1


def run(args: argparse.Namespace) -> int:
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
