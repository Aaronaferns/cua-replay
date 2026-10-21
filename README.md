# cua

A model works through a task in a web application once (discovery). That run is compiled into a capability, which a
person reviews and which is locked by a hash. Every later run replays the capability in a browser with no model. When
replay fails it says which kind of failure it hit, and a human can take over the same browser session. A write (a
commit) happens at most once. The target is a local Dolibarr running in Docker.

The lock hash is a hash of a capability together with its app profile. An approval ties a second person's sign-off
and a passing validation run to that hash, so editing either file makes the approval stale. A commit is the one
irreversible click (here, Validate on a payment). If its response is lost, the run reports `COMMIT_UNKNOWN` and never
clicks again.

## What to look at

| The brief asks for                    | Where                                                                                            |
| ------------------------------------- | ------------------------------------------------------------------------------------------------ |
| Design write-up                       | `REPORT.md` (architecture, artifact schema, error handling, tenants, handoff, safety, cuts)      |
| A real model discovery run            | `evidence/discovery_lookup_customer_outstanding/` (model `claude-opus-5-5`, 5 steps, then a draft) |
| The saved artifact                    | `capabilities/dolibarr/lookup_customer_outstanding@1.0.0.json` and its `.review.md`              |
| Replay, no model, a different input   | `evidence/replay_success_other_customer/`                                                        |
| Replay hitting an exceptional state   | `evidence/replay_commit_unknown/` (lost response, `indeterminate`), `evidence/replay_policy_denied/` |
| A human takes over the live session   | `evidence/replay_captcha_handoff/`                                                               |
| Logs and traces of a run              | `events.jsonl` in each bundle; `uv run cua view evidence/ --open` renders them (see "Evidence and observability") |
| Every bundle, explained               | `evidence/README.md`                                                                             |

More: `docs/review-guide.md` (reading order for reviewers), `docs/repo-map.md` (every folder and file),
`docs/implementation-status.md` (what is built).

## Setup

Needs Python 3.12, [uv](https://docs.astral.sh/uv/), and for the live target Docker and bash. On macOS and Linux the
target scripts run directly.

On Windows the CLI runs natively and starts the target through `wsl -d Ubuntu`, so you need a WSL distribution named
`Ubuntu` in which `docker` works (with Docker Desktop, turn on WSL integration for it; for another name set the
environment variable `CUA_WSL_DISTRO`). Clone to a short path such as `C:\src\cua`, because a deep path can exceed the
path-length limit while `uv sync` unpacks dependencies. WSL stops an idle distribution and the containers with it, so
keep a WSL terminal open, or run `wsl -d Ubuntu -- sleep infinity` in a spare window, while you work.

```bash
uv sync
uv run playwright install chromium
cp .env.example .env     # PowerShell: Copy-Item .env.example .env
```

The example values work as-is for the local target:

| Variable                                                                           | What it is for                                                                                       | Default works?                    |
| ---------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- | --------------------------------- |
| `ANTHROPIC_API_KEY`                                                                | Live discovery only (Anthropic is the only model provider). Replay and validation never call a model | Empty is fine; see `--transcript` |
| `DOLIBARR_PORT`                                                                    | Port of the local target                                                                             | Yes (8085)                        |
| `DOLIBARR_DB_ROOT_PASSWORD`, `DOLIBARR_DB_PASSWORD`, `DOLIBARR_ADMIN_PASSWORD`     | Creating and seeding the local target                                                                | Yes                               |
| `DOLIBARR_API_KEY`                                                                 | Admin REST key for fixtures, tests and evidence ground truth; never used by discovery or replay      | Yes                               |
| `CUA_RUN_LOGIN`, `CUA_RUN_PASSWORD`                                                | The account automation logs in as (a scripted login; the model never sees it)                        | Yes                               |
| `CUA_NOPERM_PASSWORD`                                                              | A user without invoice or customer rights, for the access-denied case                               | Yes                               |

If you change a password, do it before the first `uv run cua demo up`, because the seeded database keeps the
password hashes it was created with.

## Quickstart (no API key)

This replays the approved capability. To run the agent on a goal and replay what it produced, see the next section.

| Step | Command                                                                                     | Expect                                                                                                   | Time                           |
| ---- | ------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- | ------------------------------ |
| 1    | `uv run cua demo up`                                                                        | `target ready: http://localhost:8085/index.php` (first run pulls the images and seeds the data)          | ~3 min first time, ~20 s after |
| 2    | `uv run cua capabilities`                                                                   | Four rows: the two capabilities `approved` with lock hashes, and their `(draft)` files `unapproved`      | seconds                        |
| 3    | `uv run cua replay dolibarr.lookup_customer_outstanding --input customer_code=CU2601-00902` | `success`, `outstanding_amount = 355.20`                                                                 | ~30 s                          |
| 4    | `uv run cua validate dolibarr.record_invoice_payment`                                       | `5/5 passed; run <id>` (resets the target per case), including the lost-response case ending `COMMIT_UNKNOWN` | ~5 min                    |
| 5    | `uv run cua lint`                                                                           | `clean`                                                                                                  | seconds                        |

Add `--headed` to step 3 to watch the browser. Port 8085 taken? Set `DOLIBARR_PORT` in `.env` before step 1 (but see
the transcript note below). Then read `docs/review-guide.md`.

## Run the agent, then replay its result

Run the agent on a goal, then replay the artifact that run produced. With no API key, step 2 feeds the recorded
transcript back in place of the model. Nothing calls the API, but the browser, the live target and the compiler all
run for real. With `ANTHROPIC_API_KEY` set, drop the `--transcript` argument and the model works live. The recorded
transcripts navigate to `localhost:8085`, so a transcript run needs `DOLIBARR_PORT=8085`. If something else holds
8085, free it for the demo or run discovery live with a key.

1. Start the target and reset it to the seeded baseline.

   ```bash
   uv run cua demo up
   ```

2. Discover. Expect `outcome: done`, `steps: 5`, `output: outstanding_amount = ...199.75...`, and the run folder on
   the `run:` line (`runs/<run_id>/`, with `trace.json`, `transcript.json`, `events.jsonl`, `screenshots/` and the
   compiled `capability.draft.json`).

   ```bash
   uv run cua discover lookup_customer_outstanding --goal "Find the current outstanding bill amount of the customer with customer code {{customer_code}}." --input customer_code=CU2601-00901 --transcript evidence/discovery_lookup_customer_outstanding/transcript.json
   ```

3. Put that run's artifact where replay looks (`capabilities/<app>/`), under a new name so it cannot collide with the
   approved `lookup_customer_outstanding`. Compilation is deterministic, so this gives the same flow as the run's
   `capability.draft.json`. Only the id, the title and the `discovered_by` label differ.

   ```bash
   uv run cua compile runs/<run_id>/trace.json --name dolibarr.lookup_outstanding_demo --out capabilities/dolibarr/lookup_outstanding_demo@1.0.0.draft.json
   uv run cua review dolibarr.lookup_outstanding_demo     # the review card, beside the draft
   ```

4. Replay it, no model, for a customer discovery never saw, then for a code that does not exist. A draft is not
   approved, so replay refuses it unless `--allow-draft`, which runs it with a warning.

   ```bash
   uv run cua replay dolibarr.lookup_outstanding_demo --allow-draft --input customer_code=CU2601-00902   # success, outstanding_amount = 355.20
   uv run cua replay dolibarr.lookup_outstanding_demo --allow-draft --input customer_code=CU0000-0000    # business_outcome (customer_not_found)
   ```

5. Look at the evidence of both runs. The replay prints its run id as `run replay-...`.

   ```bash
   uv run cua view runs/<run_id> --open
   uv run cua view runs/<replay run id> --open
   ```

6. Remove the demo artifact by deleting `capabilities/dolibarr/lookup_outstanding_demo@1.0.0.draft.json` and
   `capabilities/dolibarr/lookup_outstanding_demo@1.0.0.review.md`.

### Human handoff, live

```bash
uv run cua make-evidence dolibarr.lookup_customer_outstanding --cases alpha_two_unpaid_invoices --handoff --out runs/evidence/
```

This turns the login CAPTCHA on and opens a headed browser. Replay meets the CAPTCHA, stops, and prints
`console: <url>  (claim, then resume / give the verdict)`. Open the console, claim control, solve the CAPTCHA in that
same browser window, then resume. Replay continues to `success`. The bundle lands in
`runs/evidence/replay_captcha_handoff/` (compare `evidence/replay_captcha_handoff/`). For a human verdict on
`COMMIT_UNKNOWN`, use `--console --cases lost_response_after_commit` on `dolibarr.record_invoice_payment`, again with
`--out runs/evidence/`. After a run, `uv run cua console <bundle>` serves the console in review mode for a closed
bundle that has `interventions/`, for example to record a `COMMIT_UNKNOWN` verdict (loopback only).

## Without Docker or an API key

- Without Docker, read the committed `evidence/` (start at `evidence/README.md`) or render it with `cua view` (see
  "What to look at"). `uv run cua capabilities`, `uv run cua lint`, `uv run cua compile` on a committed trace and the
  offline tests (`uv run pytest -q -m "not live and not model and not human"`) all run.
- Without an API key, everything runs except live discovery and the `model` tests. Discovery takes
  `--transcript <transcript.json>` to replay recorded model turns.

## Payment discovery (a commit)

The payment discovery clicks Validate, so it needs `--policy dolibarr-sandbox-commit` (the default policy only allows
reads, and its effect ceiling would deny the click) and `--approve-commits`, which prints a console URL where you
approve each commit. It records a real payment, so run `uv run cua demo up` afterwards to reset the target.

A transcript repeats the clicks it recorded, so give it the invoice it was recorded on, the seed's
`CUA-SEED-ALPHA-2`. Dolibarr numbers invoices by month when the seed validates them, so the reference differs per
target. The first command below prints the one your target gave it.

```bash
uv run python -c "from cua.replay.commands import _seed_invoice_ref as r; print(r('CUA-SEED-ALPHA-2'))"
uv run cua discover record_invoice_payment --goal "Record a payment of {{amount}} on customer invoice {{invoice_ref}} using payment method {{method}} into bank account {{bank_account}}, confirm it, and read the reference of the payment that was created." --input invoice_ref=<the ref printed above> --input amount=10.00 --input method=Cash --input "bank_account=CUA Seed Operating Account (EUR)" --output payment_ref:string --transcript evidence/discovery_record_payment/transcript.json --policy dolibarr-sandbox-commit --approve-commits --headed
```

A capability the CLI does not know declares its outputs with `--output name:decimal`. `--help` on any command lists
the rest of its options (for live discovery: `--effort`, `--screenshots`, `--max-steps`, `--wall-clock`).

## From draft to approved capability

`review`, `validate` and `approve` find a capability by name under `capabilities/<app>/` only, and `approve` refuses
a `.draft.json`. Use a new name. A new version of an existing capability would become the default for `replay`
without `--version`, and replay refuses it until it is approved. Changing the two approved capabilities makes their
approvals stale.

```bash
uv run cua compile runs/<run_id>/trace.json --name dolibarr.<capability> --version <version> --out capabilities/dolibarr/<capability>@<version>.draft.json
uv run cua review dolibarr.<capability>
# after review: rename the file to <capability>@<version>.json and write <capability>@<version>.tests.json (the suite)
uv run cua validate dolibarr.<capability>          # prints "N/M passed; run <run id>; lock ..."
uv run cua approve dolibarr.<capability> --version <version> --approver <not the discoverer> --run <run id printed by validate>
```

## Evidence and observability

Every run writes a bundle to `runs/<run_id>/`, and curated copies live in `evidence/`. A bundle holds `events.jsonl`
(one event per model action, replay step, policy check, change of who holds control and human action), the
screenshots, the model transcript and `result.json`. The two commands below read a closed bundle and do not change
it.

`cua view` puts the steps and any human handoffs on one timeline, coloured by who acted, with the time spent waiting
for a person shown apart from the time they held control. Each step gets a card with the action, the model's stated
reason, the element it acted on, the policy decision and the screenshot. On a discovery run the page also shows what
each model turn compiled into: the replay steps, the identity checks the compiler added, the locators replay will try
in order, and which turns were dropped and why.

```bash
uv run cua view evidence/ --open                              # one HTML trace per bundle + an index, in runs/views/
uv run cua view evidence/replay_commit_unknown --out runs/t.html   # one self-contained page (screenshots embedded)
uv run cua export-otel runs/<run_id> --out runs/trace.otlp.json    # write the OTLP/HTTP JSON payload, send nothing
uv run cua export-otel evidence/ --endpoint http://localhost:4318  # send it to Jaeger or any OTLP collector
```

`cua export-otel` takes exactly one of `--out` (write the payload, send nothing) or `--endpoint` (send it). It runs
the evidence lint over the bundle and the payload first and refuses on any finding, and it never sends screenshots,
DOM snapshots or page text. Headers come from `--header K=V` or `OTEL_EXPORTER_OTLP_HEADERS` and are never printed.

## Tests

Run from the repository root. Three markers gate tests:

- `live`: skipped when the target is not running.
- `model`: calls the API, skipped without `ANTHROPIC_API_KEY`.
- `human`: needs a person at the browser, runs only with `CUA_HUMAN=1`.

```bash
uv run pytest -q -m "not live and not model and not human"   # offline, ~1 min
uv run pytest -q -m "not model"                              # with the target running, ~10-12 min
```

There is one folder per component under `tests/` (`docs/repo-map.md` lists them). Run one with
`uv run pytest -q tests/<folder>`, adding `-m "not live and not model and not human"` to stay offline.

The files worth running on their own:

```bash
uv run pytest -q tests/replay/test_replay_m4.py          # one test per failure case
uv run pytest -q tests/replay/test_commit_path.py        # payment path: single click, ledger, COMMIT_UNKNOWN
uv run pytest -q tests/replay/test_replay_imports.py     # replay never imports model or fault code
```

Validation suites (not pytest; they reset the target before each case):

```bash
uv run cua validate dolibarr.lookup_customer_outstanding             # 11 cases, ~4 min
uv run cua validate dolibarr.lookup_customer_outstanding --repeat 3  # 33 runs, ~12 min; rewrites evidence/validation_suite.json
uv run cua validate dolibarr.record_invoice_payment                  # 5 cases, ~5 min
```

Replay evidence bundles are regenerated from a suite, which resets the target per case. Always pass `--out`. The
default is `evidence/`, and writing there overwrites the committed bundles.

```bash
uv run cua make-evidence dolibarr.lookup_customer_outstanding --cases beta_other_customer --out runs/evidence/
```
