"""`cua console`: serve the operator console over a run bundle's interventions (review mode: queue, context, verdicts).

A live run starts its own console from the interpreter (`ConsoleServer(hub)`); this command is for looking at a closed
bundle and recording a `resolve_commit` verdict afterwards. Loopback only, no authentication.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path


def cmd_console(args: argparse.Namespace) -> int:
    from cua.control.console import ConsoleServer, InterventionHub  # noqa: PLC0415

    try:
        hub = InterventionHub.review(Path(args.run))
        server = ConsoleServer(hub, host=args.host, port=args.port)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    async def main() -> None:
        async with server.running() as url:
            print(f"console: {url}  (loopback only, no authentication; Ctrl+C to stop)", flush=True)
            await asyncio.Event().wait()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("console", help="serve the operator console for a run bundle (loopback only)")
    p.add_argument("run", help="a run bundle folder containing interventions/")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(func=cmd_console)
