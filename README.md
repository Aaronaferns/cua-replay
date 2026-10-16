# cua

A model explores a web application once (discovery); the run is compiled into a reviewed, hash-locked capability;
every later run replays that capability in a browser with no model. Failures are classified, a human can take over
the same session, and a write (commit) is performed at most once. The target is a local Dolibarr in Docker.

- `REPORT.md`: design, taxonomy, safety table, cuts.
- `evidence/README.md`: each committed evidence bundle and where to look.
- `docs/repo-map.md`: every folder and file and what it is for.
- `docs/review-guide.md`: where to start and in what order, if you are reviewing.
- `docs/implementation-status.md`: what is built.



## Reviewer quickstart

Needs Docker, bash and [uv](https://docs.astral.sh/uv/); no API key. On macOS and Linux the target scripts run
directly. On Windows the CLI runs natively and starts the target through WSL: you need a WSL distribution named
`Ubuntu` in which `docker` works (with Docker Desktop, turn on WSL integration for it; another name: set the
environment variable `CUA_WSL_DISTRO`), and a short clone path such as `C:\src\cua`.


| Step | Command                                                                                     | Expect                                                                                          | Time                           |
| ---- | ------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- | ------------------------------ |
| 1    | `uv sync` then `uv run playwright install chromium`                                         | Dependencies and a browser installed                                                            | 1–3 min                        |
| 2    | `cp .env.example .env` (PowerShell: `Copy-Item .env.example .env`)                          | The example values work as-is for the local sandbox                                             | —                              |
| 3    | `uv run cua demo up`                                                                        | `target ready: http://localhost:8085/index.php` (first run pulls the images and seeds the data) | ~3 min first time, ~20 s after |
| 4    | `uv run cua capabilities`                                                                   | Two capabilities, both `approved`                                                               | seconds                        |
| 5    | `uv run cua replay dolibarr.lookup_customer_outstanding --input customer_code=CU2601-00902` | `success`, `outstanding_amount = 355.20`                                                        | ~30 s                          |
| 6    | `uv run cua validate dolibarr.record_invoice_payment`                                       | `5/5 passed`, including the lost-response case ending `COMMIT_UNKNOWN`                          | ~5 min                         |
| 7    | `uv run cua lint`                                                                           | `clean`                                                                                         | seconds                        |


Add `--headed` to step 5 to watch the browser. Port 8085 is taken? Set `DOLIBARR_PORT` in `.env` before step 3.
Then read `docs/review-guide.md`. Without Docker, steps 4 and 7 and the offline tests still run (below).

## Prerequisites

- Python 3.12 and [uv](https://docs.astral.sh/uv/).
- Docker (on Windows: Docker inside WSL2) for the live target.
- An LLM API key, for discovery only. Replay and validation never call a model.
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



## Discovery

Discovery needs the target running. With no API key, replay the recorded model run from its transcript: the same
model choices, no API calls.

```bash
uv run cua discover lookup_customer_outstanding --goal "Find the current outstanding bill amount of the customer with customer code {{customer_code}}." --input customer_code=CU2601-00901 --transcript evidence/discovery_lookup_customer_outstanding/transcript.json --headed
```

Expect `outcome: done`, 5 steps, 199.75, and a run under `runs/` with `trace.json`, `transcript.json`,
`events.jsonl`, `screenshots/` and the compiled `capability.draft.json`.

The payment discovery clicks Validate, so it needs `--approve-commits`: approve each commit in the console URL it
prints. It records a real payment; run `uv run cua demo up` afterwards to reset the target. A transcript repeats the
clicks it recorded, so give it the invoice it was recorded on, the seed's `CUA-SEED-ALPHA-2`:

```bash
uv run python -c "from cua.replay.commands import _seed_invoice_ref as r; print(r('CUA-SEED-ALPHA-2'))"
uv run cua discover record_invoice_payment --goal "Record a payment of {{amount}} on customer invoice {{invoice_ref}} using payment method {{method}} into bank account {{bank_account}}, confirm it, and read the reference of the payment that was created." --input invoice_ref=<the ref printed above> --input amount=10.00 --input method=Cash --input "bank_account=CUA Seed Operating Account (EUR)" --output payment_ref:string --transcript evidence/discovery_record_payment/transcript.json --approve-commits --headed
```

With `ANTHROPIC_API_KEY` in `.env`, drop `--transcript` to let the model work live. Options: `--effort`,
`--screenshots` (send images as well as text), `--max-steps`, `--wall-clock`. A new goal declares its outputs:
`--output name:decimal`.

From a run to an approved capability:

```bash
uv run cua compile runs/<run_id>/trace.json --name dolibarr.<capability> --out runs/<run_id>/recompiled.draft.json
uv run cua review dolibarr.lookup_customer_outstanding
uv run cua validate dolibarr.<capability>
uv run cua approve dolibarr.<capability> --version 1.0.0 --approver <not the discoverer> --run <validation run id>
```

To approve a new capability, copy its draft into `capabilities/dolibarr/` as `<name>@<version>.json` and add a
`<name>@<version>.tests.json` suite. Use a new name or version: changing the two approved capabilities makes their
approvals stale.

Compilation is reproducible: compiling the committed trace gives `capabilities/dolibarr/lookup_customer_outstanding@1.0.0.draft.json`
byte for byte (`test_compile_from_trace_reproduces_draft`):

```bash
uv run cua compile evidence/discovery_lookup_customer_outstanding/trace.json --name dolibarr.lookup_customer_outstanding --out recompiled.json
```

It does not match `evidence/discovery_lookup_customer_outstanding/capability.draft.json`, which is what the compiler
produced on the day of the run; the compiler has improved since (it now adds the not-found and access-denied
outcomes).

Other commands: `uv run cua discover` (needs the key), `uv run cua compile`, `uv run cua review`,
`uv run cua approve`, `uv run cua console` (operator console for a run bundle, loopback only). Use
`uv run cua <command> --help` for the arguments.

## Tests

Run from the repository root. Tests marked `live` are skipped when the target is not running; `model` tests call the
API; `human` tests need a person at the browser.

```bash
uv run pytest -q -m "not live and not model and not human"   # offline, ~1 min
uv run pytest -q -m "not model"                              # with the target running, ~10-12 min
```

One component at a time (add `-m "not live and not model and not human"` to stay offline):


| Component                                             | Command                              |
| ----------------------------------------------------- | ------------------------------------ |
| Discovery agent: loop, executor, commit gate, adapter | `uv run pytest -q tests/agent`       |
| Artifact: schema, lock hash, approvals, classifier    | `uv run pytest -q tests/artifact`    |
| Compiler: trace to capability, review card            | `uv run pytest -q tests/compiler`    |
| Replay: interpreter, ledger, failure cases, CLI       | `uv run pytest -q tests/replay`      |
| Human handoff: lease, console, resume, capture        | `uv run pytest -q tests/control`     |
| Safety: policy, network, redaction                    | `uv run pytest -q tests/safety`      |
| Evidence: writer, make-evidence, lint, demo           | `uv run pytest -q tests/evidence`    |
| Browser surface                                       | `uv run pytest -q tests/surface`     |
| Fault injection                                       | `uv run pytest -q tests/faults`      |
| Target scripts: seed, reset, ports                    | `uv run pytest -q tests/target`      |
| End to end                                            | `uv run pytest -q tests/integration` |


The files worth running on their own:

```bash
uv run pytest -q tests/replay/test_replay_m4.py          # one test per failure case
uv run pytest -q tests/replay/test_commit_path.py        # payment path: single click, ledger, COMMIT_UNKNOWN
uv run pytest -q tests/replay/test_replay_imports.py     # replay never imports model or fault code
uv run pytest -q tests/artifact/test_artifact_lock_approval.py
uv run pytest -q tests/control/test_lease.py tests/control/test_resume.py
uv run pytest -q -k "lost_response or session_expiry"    # by name
```

Validation suites (not pytest; they reset the target before each case):

```bash
uv run cua validate dolibarr.lookup_customer_outstanding             # 11 cases, ~4 min
uv run cua validate dolibarr.lookup_customer_outstanding --repeat 3  # flake check; rewrites evidence/validation_suite.json
uv run cua validate dolibarr.record_invoice_payment                  # 5 cases, ~5 min
```

