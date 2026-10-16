# Repository map

Every tracked folder and file, with one line on what it is for. Start with `README.md` (how to run it) and
`REPORT.md` (what it does and how it is shown); come here to find where something lives.

## How the parts connect

```
goal ──► src/cua/agent (discovery, model) ──► src/cua/compiler ──► capabilities/<app>/*.draft.json
                                                                        │ review card, test suite
                                                                        ▼
                         approvals/<app>/*.approval.json ◄── cua approve (lock hash)
                                                                        │
inputs ──► src/cua/replay (no model) ──► RunResult + run bundle (runs/, promoted to evidence/)
              │ uses: surface (browser), artifact (schema, classification), control (human handoff),
              │       safety (policy, network, redaction), evidence (writer)
              ▼
         target/dolibarr (the application under automation, in Docker)
```

## Top level

| Path | What it is |
|---|---|
| `README.md` | Prerequisites, setup, what runs without Docker or an API key, demo commands |
| `REPORT.md` | The submission report: architecture, schema, error handling, handoff, safety, cuts |
| `pyproject.toml` | Package metadata, the five runtime dependencies, the `cua` entry point, pytest markers |
| `uv.lock` | Exact dependency versions for `uv sync` |
| `.env.example` | Template for `.env` (ports, target passwords, API key); `.env` itself is git-ignored |
| `.gitignore` | Keeps `.env`, `runs/`, database dumps, caches and agent worktrees out of git |
| `.gitattributes` | LF line endings for all text files; images and PDFs marked binary |

Not tracked but present when you work: `.env` (secrets), `runs/` (every run's bundle; the curated ones are copied to
`evidence/`), `target/dolibarr/dumps/` (the post-seed database dump), `.venv/`, `.claude/` (agent working state).

## `src/cua/` — the system

| File | What it is |
|---|---|
| `__init__.py` | Package marker and one-line description |
| `cli.py` | The `cua` command: wires up each package's `commands.py` |
| `config.py` | Settings from the environment and `.env`; secrets are held as `SecretStr` |

### `src/cua/agent/` — discovery (the only place a model is used)

| File | What it is |
|---|---|
| `actions.py` | The closed set of actions the model may choose (navigate, click, fill, select, extract, done, request_help) |
| `model.py` | The model interface the loop depends on, independent of provider; renders observations to text |
| `anthropic_adapter.py` | The Claude adapter: system prompt, tool definitions, caching, refusal and error handling |
| `scripted_adapter.py` | A stand-in model that replays a fixed action list or a recorded `transcript.json` (tests, no-credit reruns) |
| `loop.py` | The observe → ask model for one action → execute → record loop, with its stop conditions |
| `executor.py` | Turns a model action into a guarded browser call: placeholder-to-token substitution, effect class, commit gate |
| `stuck.py` | Detects going in circles (same screen signature with no progress) |
| `discovery.py` | One discovery run end to end: loop → run bundle → compiled draft |
| `commands.py` | `cua discover` |

### `src/cua/compiler/` — from recorded run to capability draft

| File | What it is |
|---|---|
| `trace.py` | `RecordedTrace`, the contract between discovery and the compiler |
| `recorder.py` | Builds the trace during discovery: actions, page states, where inputs appear |
| `locators.py` | Ranked locator candidates for an element (role and name, text, structure) with match counts |
| `compile.py` | Trace → capability draft: keeps performed actions, lifts inputs to parameters, picks locators, adds checks |
| `review_card.py` | Renders the Markdown review card a second person reads before approving |

### `src/cua/artifact/` — the capability format and its rules

| File | What it is |
|---|---|
| `schema.py` | Pydantic models for a capability and an app profile (frozen at M3) |
| `lock.py` | Canonical JSON and the lock hash over capability + profile that approval binds to |
| `approval.py` | Approval records (sidecar files) and the checks that an approval is current |
| `validate.py` | Static checks of a capability against its profile before anything runs |
| `suite.py` | The `<capability>.tests.json` format: cases, expected statuses, faults, oracles |
| `predicates.py` | The small predicate language used in state signatures and checks |
| `classify.py` | Matches an observed screen to a profile state (pure, deterministic) |
| `propose.py` | Proposes a new state for a screen the profile does not know |
| `screen.py` | Parses a screen's text outline into surface-neutral nodes |
| `routes.py` | Decides whether a request is a commit (method, path, query, form body) |
| `names.py` | The one normalisation rule for accessible names and display text |
| `values.py` | Typed inputs and outputs: checking inputs, parsing displayed text into decimals, dates, strings |

### `src/cua/replay/` — model-free execution

| File | What it is |
|---|---|
| `interpreter.py` | Runs a capability step by step: preflight, classify, resolve, guard, act, verify, wait |
| `resolver.py` | Finds exactly one element per step; fallbacks only when the fingerprint matches |
| `classifier.py` | Waits for the page to settle, then classifies it |
| `checks.py` | Evaluates predicates against the live page |
| `effects.py` | The effect class (none, read, commit) replay assigns each step |
| `commit_ledger.py` | SQLite `request_id` ledger: PREPARED → ATTEMPTED → DONE / UNKNOWN; at most one commit per request |
| `result.py` | `RunResult`: status, outcome, outputs, recoveries, drift, failure details |
| `suite.py` | Compares a run's result with a test-suite case |
| `commands.py` | `cua replay`, `compile`, `approve`, `capabilities`, `validate` (incl. `--repeat`) |

### `src/cua/surface/` — perceiving and acting on the browser

| File | What it is |
|---|---|
| `base.py` | The `Surface` interface and observation types (keeps Playwright out of artifacts) |
| `web.py` | The Playwright implementation: snapshots, element resolution, clicks, fills, page facts |
| `snapshot.py` | Parses and scopes Playwright's accessibility snapshots |
| `login.py` | Scripted login from the profile; credentials never reach the model |
| `names.py` | Surface-side name matching, delegating to `artifact/names.py` |

### `src/cua/control/` — human handoff

| File | What it is |
|---|---|
| `interventions.py` | The intervention request written when automation needs a person |
| `lease.py` | Who may act right now (automation or human), with a generation number |
| `resume.py` | When a run may resume or be marked complete (never after a commit attempt) |
| `capture.py` | Records what the human does in the live browser, with values redacted |
| `console.py` | The loopback FastAPI operator console and the hub runs wait on |
| `approver.py` | Single-use console approvals for commits during discovery |
| `runtime.py` | Opens a browser session with every guard wired in |
| `commands.py` | `cua console`, and the helper that attaches a live console to a run |

### `src/cua/safety/` — guard rails

| File | What it is |
|---|---|
| `policy.py` | The action policy: deny by default, effect ceiling, route rules |
| `network.py` | The only way to get a browser context: blocks off-allow-list origins, redirects, websockets, downloads |
| `redact.py` | Removes secrets and personal data from anything written to disk or sent to a model |
| `lint.py` | `cua lint`: scans `evidence/` for secrets, SSNs, cookies, unmasked e-mails |

### `src/cua/evidence/` — run bundles

| File | What it is |
|---|---|
| `events.py` | The `events.jsonl` schema shared by agent, replay, human and system |
| `writer.py` | The single path that writes a run bundle (redacting as it goes) |
| `failure.py` | What a failed step leaves behind: masked screenshot and snapshot |
| `commands.py` | `cua make-evidence` (regenerate bundles from a suite) and registration of `lint` and `demo` |
| `demo.py` | `cua demo up`: start the target, wait for health, reset, print the URL |

### `src/cua/faults/` — test-only fault injection

| File | What it is |
|---|---|
| `harness.py` | Network-level faults (slow, error page, interstitial, unknown dialog, lost response); sandbox only, never imported by replay |

## `capabilities/dolibarr/` — the learned procedures

Each capability has four files. Changing the `.json` (or the app profile) changes the lock hash and voids its approval.

| File pattern | What it is |
|---|---|
| `<name>@<version>.draft.json` | The compiler's output, before review |
| `<name>@<version>.json` | The final capability replay runs (promoted from the draft) |
| `<name>@<version>.review.md` | The review card read before approval |
| `<name>@<version>.tests.json` | The test suite `cua validate` runs |

Capabilities: `lookup_customer_outstanding` (read: a customer's outstanding balance) and `record_invoice_payment`
(commit: record a payment against an invoice).

## `approvals/dolibarr/`

| File | What it is |
|---|---|
| `lookup_customer_outstanding@1.0.0.approval.json` | Approval of the lookup capability: approver, lock hash, the validation run it relied on |
| `record_invoice_payment@1.0.0.approval.json` | The same for the payment capability |

## `apps/dolibarr/`

| File | What it is |
|---|---|
| `profile.json` | Everything app-specific: login flow, screen states, commit routes, interstitials. Part of every lock hash |

## `policies/`

| File | What it is |
|---|---|
| `dolibarr-sandbox.json` | Read-only policy: allowed origin and routes, effect ceiling `read` |
| `dolibarr-sandbox-commit.json` | The same with commits allowed (used for the payment capability) |
| `dolibarr-sandbox-deny-payment.json` | Denies the payment route; used to show the policy preflight (`evidence/replay_policy_denied/`) |

## `evidence/` — committed proof

| Path | What it is |
|---|---|
| `README.md` | Index: bundle → requirement → which file to open |
| `discovery_lookup_customer_outstanding/` | A real model discovery run of the lookup task |
| `discovery_record_payment/` | Discovery of the payment task, finished from the recorded transcript |
| `replay_success_other_customer/` | Replay for another customer, no model |
| `replay_captcha_handoff/` | CAPTCHA stops replay; a person solves it in the same session; replay finishes |
| `replay_commit_unknown/` | Lost response after the payment click: one click, `COMMIT_UNKNOWN`, operator verdict |
| `replay_policy_denied/` | A denying policy stops replay before any UI action |
| `validation_suite.json` | The lookup suite run 3 times: cases and flakes |
| `evidence_lint.txt` | Output of `cua lint` over this folder |

Files inside a bundle:

| File | What it is |
|---|---|
| `manifest.json` | Run id, capability, version, lock hash, timestamps |
| `events.jsonl` | Every action and decision, one JSON line each, with rationale |
| `result.json` | The run's result (status, outcome, outputs, recoveries, drift; for discovery, token usage) |
| `ground_truth.json` | REST API state before and after (replay bundles), and the declared faults |
| `trace.json` | The recorded discovery trace (discovery bundles) |
| `transcript.json` | The full model conversation (discovery bundles) |
| `capability.draft.json` / `capability.json` | The capability compiled from, or replayed in, this run |
| `screenshots/NN-*.png` | A screenshot per discovery step |
| `failure/*.png`, `failure/*.snapshot.json` | Masked screenshot and snapshot where the run stopped |
| `interventions/*.json` | Requests for a person (CAPTCHA, unknown commit, commit approval) |
| `verdicts/*.json` | The operator's verdict on an unknown commit |

## `target/dolibarr/` — the application under automation

| File | What it is |
|---|---|
| `README.md` | How the target runs, ports per checkout, Windows/WSL notes |
| `docker-compose.yml` | Dolibarr and MariaDB containers |
| `_env.sh` | Shared shell setup: loads `.env`, names the compose project after the port |
| `up.sh` | Start the containers and wait until healthy |
| `reset.sh` | Restore the post-seed database dump (about 9 s); `reset.sh dump` saves it |
| `captcha.sh` | Turn the login CAPTCHA on or off |
| `session.sh` | Expire a browser session server-side (session-expiry tests) |
| `seed.py` | Create the known users, customers and invoices through the REST API |
| `fixtures.json` | The seed data |
| `dolirest.py` | Small REST client and the ground-truth helpers (outstanding amounts, payments) |
| `dolictl.py` | Runs the scripts from Python, through WSL on Windows |
| `livetarget.py` | `reset_to_baseline()` and session expiry for live tests |

## `docs/`

| File | What it is |
|---|---|
| `repo-map.md` | This file |
| `review-guide.md` | A step-by-step plan for reviewing the repository |
| `architecture-review-v2.md` | The design review: requirements coverage, options considered, chosen architecture |
| `build-plan-v2.md` | The milestone plan (M0–M8), gates and cut list the build followed |
| `contracts.md` | Interfaces between subsystems |
| `implementation-status.md` | Per-milestone status, tests, evidence and commits |
| `architecture.svg` | The architecture diagram |
| `archive/architecture-design-v2.svg` | An earlier version of the diagram |

## `spikes/` — early experiments (M0)

Throwaway scripts that answered design questions before the build. Kept as the record behind early decisions; nothing
in `src/` imports them.

| File | Question it answered |
|---|---|
| `README.md` | Index of spikes and their results |
| `s2_process_model.py` | Can one event loop host the console and a headed browser together? |
| `s6_escape_hatches.py` | What gets past a deny-all network route? |
| `s7_name_match.py` | Which name-matching rule works on Dolibarr's real accessible names? |
| `s9_payment.py` | Can a payment be committed through the UI, and does a lost response leave exactly one? |
| `dolibarr/README.md` | Findings on Dolibarr itself |
| `dolibarr/docker-compose.yml`, `up.sh`, `probe_env.sh`, `captcha.sh` | First version of the target setup |
| `dolibarr/api_probe.py`, `api_setup.py` | REST API as a ground-truth channel; fixture setup |
| `dolibarr/s1_snapshot.py` | Accessibility snapshots and acting by ref on real screens |
| `dolibarr/s2_flows.py` | Reproducing the brief's runtime conditions on the real app |
| `dolibarr/s3_search_payment.py` | Near-miss search rows and the payment endpoints |
| `dolibarr/s4_captcha.py` | The login CAPTCHA as the human-only trigger |

## `tests/` — automated tests

Folders mirror `src/cua/`. Markers: `live` needs the Docker target, `model` calls the API, `human` needs a person.
Offline run: `uv run pytest -m "not live and not model and not human"`.

| Path | What it tests |
|---|---|
| `conftest.py` | Shared fixtures and markers |
| `test_contracts.py` | The cross-subsystem contracts in `docs/contracts.md` |
| `agent/` | Action schema, Claude adapter, executor tokens, commit gate, loop, stuck detection, `cua discover`; `scripts.py` holds scripted runs |
| `artifact/` | Schema and its freeze, lock hash and approval, classifier, predicates, routes, names, values; `fixtures/` holds sample outlines and the frozen schema |
| `compiler/` | Compilation (incl. byte-for-byte reproduction of the committed draft), commit steps, locators, state proposals, review card; `fixtures/record_payment_trace.json` is a recorded trace |
| `replay/` | Interpreter, failure taxonomy (`test_replay_m4.py`), commit path and ledger, handoff, settling, CLI, validation, import rules, live replays; `fakes.py`, `pay_world.py`, `livehelp.py` are test doubles and helpers |
| `control/` | Lease, interventions, resume rules, console, approver, capture, runtime |
| `safety/` | Policy, network containment, redaction, error-message hygiene, import rules |
| `evidence/` | Bundle writer, failure evidence, make-evidence, evidence lint and README smoke check, demo |
| `faults/` | Fault harness and the live lost-response case |
| `surface/` | Web surface, snapshots, login, tables, payment form, settling; `fixtures/*.html` and `fakesite.py` are an offline stand-in site |
| `target/` | Seed, reset, session expiry, per-port isolation; `fake_dolibarr.py` is an offline stand-in |
| `integration/` | End-to-end checks across subsystems (M1 acceptance, live capture, live make-evidence) |
