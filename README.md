# cua

A model explores a web application once (discovery); the run is compiled into a reviewed, hash-locked capability;
every later run replays that capability in a browser with no model. Failures are classified, a human can take over
the same session, and a write (commit) is performed at most once. The target is a local Dolibarr in Docker.

- `REPORT.md`: design, taxonomy, safety table, cuts.
- `evidence/README.md`: each committed evidence bundle and where to look.
- `docs/repo-map.md`: every folder and file and what it is for.
- `docs/review-guide.md`: where to start and in what order, if you are reviewing.
- `docs/implementation-status.md`: what is built.

## Prerequisites

- Python 3.12 and [uv](https://docs.astral.sh/uv/).
- Docker (on Windows: Docker inside WSL2) for the live target.
- An Anthropic API key, for discovery only. Replay and validation never call a model.
- Windows: clone to a short path (for example `C:\src\cua`); a deep path can exceed the path-length limit while
  `uv sync` unpacks dependencies. On Windows the `cua` CLI runs natively and reaches Docker through `wsl -d Ubuntu`;
  to run the CLI inside WSL instead, install uv there first.

## Setup

```bash
uv sync
uv run playwright install chromium
cp .env.example .env     # PowerShell: Copy-Item .env.example .env
```

Fill in `.env` (ports and passwords for the local target; the API key only if you will run discovery).

## What runs without Docker, and without a key

- Without Docker: read the committed `evidence/` (start at `evidence/README.md`), run `uv run cua lint`, and run the
  offline tests: `uv run pytest -q -m "not live and not model and not human"`.
- Without an API key: everything except `uv run cua discover` (replay, validate, make-evidence, console).
  Discovery can be replayed from a recorded transcript with `--transcript`.

## Demo (target running)

Start the target, wait for health, reset it to the seeded baseline:

```bash
uv run cua demo up
```

Windows: Docker lives in WSL2, which stops an idle distribution and the containers with it. Keep a WSL terminal open
(or run `wsl -- sleep infinity` in a spare window) while you work.

Replay a read capability (no model), then the suite, then the lint:

PowerShell:

```powershell
uv run cua capabilities
uv run cua replay dolibarr.lookup_customer_outstanding --input customer_code=CU2601-00902
uv run cua validate dolibarr.lookup_customer_outstanding --repeat 3
uv run cua lint
```

bash:

```bash
uv run cua capabilities
uv run cua replay dolibarr.lookup_customer_outstanding --input customer_code=CU2601-00902
uv run cua validate dolibarr.lookup_customer_outstanding --repeat 3
uv run cua lint
```

Regenerate evidence bundles from a suite (resets the target per case):

```bash
uv run cua make-evidence dolibarr.lookup_customer_outstanding --cases beta_other_customer
```

Other commands: `uv run cua discover` (needs the key), `uv run cua compile`, `uv run cua review`,
`uv run cua approve`, `uv run cua console` (operator console for a run bundle, loopback only). Use
`uv run cua <command> --help` for the arguments.

## Tests

```bash
uv run pytest -q -m "not model"      # live tests are skipped when the target is not reachable
```
