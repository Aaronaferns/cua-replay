# Review guide

A plan for reviewing this repository from a cold start, in about three hours. Each pass gives you a mental model
before the next one asks you to read code. Stop after any pass and you still have a coherent picture.

**The one idea to hold:** a model learns a task once; a person approves what it learned; a program with no model
in it repeats the task. Everything else is about making that repetition honest (it says what really happened)
and safe (it never pays twice).

| Pass | Time | You read | You come away knowing |
|---|---|---|---|
| 1 Orientation | 15 min | README, REPORT §1, repo map | What the system claims and where things live |
| 2 Evidence first | 40 min | `evidence/` bundles | What it actually did, without trusting the code |
| 3 Run it | 20 min | Your terminal | That it works on your machine |
| 4 Follow one run | 60 min | Replay code, top to bottom | How a run works |
| 5 Safety audit | 30 min | The commit and handoff paths | Whether the safety claims hold |
| 6 Tests as spec | 20 min | Four test files | What behaviour is pinned |

## Pass 1: orientation (15 min)

1. `README.md`: what it is and how to run it.
2. `REPORT.md` §1 Architecture and §3's taxonomy table. Skim the rest; you will come back to it.
3. `docs/repo-map.md`: read the diagram at the top, then skim the headings. Don't read every row.

Write down the four statuses a run can end in (success, business outcome, failure, indeterminate) and the two
capabilities (a read and a payment). You will see them everywhere.

## Pass 2: evidence first (40 min)

Read what the system produced before reading how. Open `evidence/README.md`, then the bundles in this order.

1. **`discovery_lookup_customer_outstanding/`**: the model learning a task.
   - `transcript.json`: read the model's turns. Note that it writes `{{customer_code}}`, never a real value.
   - `trace.json`: what the recorder kept from those turns.
   - `capability.draft.json`: what the compiler made of it.
2. **`capabilities/dolibarr/lookup_customer_outstanding@1.0.0.review.md`**: the page a human approves. Then the
   matching `.json` (the capability itself) and `.tests.json` (its test suite).
3. **`approvals/dolibarr/lookup_customer_outstanding@1.0.0.approval.json`**: who approved what, bound to which lock
   hash, relying on which validation run.
4. **`replay_success_other_customer/`**: the same capability run for a different customer, with no model.
   `result.json` should have outputs and an empty `drift`.
5. **`replay_commit_unknown/`**: the most important bundle. Read `events.jsonl` in order and find
   `commit_attempted`, then the single click. Then `result.json` (`indeterminate`, `COMMIT_UNKNOWN`,
   `retry_safe: false`), `verdicts/`, and `ground_truth.json` (payments before 0, after 1).
6. **`replay_captcha_handoff/`**: a person takes over mid-run. `interventions/`, then `events.jsonl` for the lease
   generations, then `ground_truth.json`.
7. **`replay_policy_denied/`**: the run stops before touching the page.
8. `validation_suite.json` and `evidence_lint.txt`.

**Questions to answer here:**
- Does each bundle's `ground_truth.json` (the app's REST API) agree with what `result.json` claims?
- Is anything in a bundle that should not be, such as a password, a cookie, or a real e-mail?
- `discovery_record_payment/result.json` says `model: scripted`. Read REPORT §7 for why, and decide whether that is
  disclosed clearly enough.

## Pass 3: run it (20 min)

Without Docker:

```bash
uv sync
uv run pytest -q -m "not live and not model and not human"
uv run cua capabilities
uv run cua lint
uv run cua review dolibarr.lookup_customer_outstanding
```

With Docker (Windows: Docker inside WSL2):

```bash
uv run playwright install chromium
uv run cua demo up
uv run cua replay dolibarr.lookup_customer_outstanding --input customer_code=<a seeded code> --headed
uv run cua validate dolibarr.record_invoice_payment
```

`--headed` lets you watch the browser. The payment suite takes a few minutes, because Dolibarr's confirmation step
is slow.

## Pass 4: follow one run (60 min)

Read along the path a single `cua replay` takes. Don't read packages front to back.

1. `src/cua/replay/commands.py`: `cmd_replay` → `run_replay` → `_execute`. Note what is loaded (capability,
   profile, approval, policy) before anything runs.
2. `src/cua/replay/interpreter.py`: start at `replay()` at the bottom, then `_Run.run()`. Then, in order:
   - `_preflight`, `_begin_ledger`, `_enter`: everything checked before the first click;
   - `_run_step`: the per-step cycle;
   - `_settle` and `_acceptor`: how it decides what page it is on;
   - `_act_on_verdict`, `_recover`, `_escalate`: what happens on an unexpected page.
3. `src/cua/replay/resolver.py`: how a step finds exactly one element. This is where flaky UI automation usually
   breaks.
4. `src/cua/artifact/classify.py` with `apps/dolibarr/profile.json` open beside it: how a screen becomes a state.
5. `src/cua/replay/result.py`: what the caller gets back.

Then the learning side, more briefly:

6. `src/cua/agent/loop.py`: `AgentLoop.run`, one turn at a time.
7. `src/cua/agent/executor.py`: `Executor.execute` and `effect_of`. Every model action passes through here.
8. `src/cua/compiler/compile.py`: `compile_trace`, trace in, capability out.
9. `src/cua/agent/anthropic_adapter.py`: the only file that talks to a model. Read the system prompt.

**Skip on a first read:** `spikes/` (early experiments), `tests/**/fixtures`, the inline HTML in
`control/console.py`, and `target/` (Docker plumbing).

## Pass 5: safety audit (30 min)

For each claim, find the line that enforces it and the test that pins it. If you can't find both, that is a finding.

Code paths in this table are under `src/cua/`; test paths start with `tests/`.

| Claim | Start reading at | Pinned by |
|---|---|---|
| Replay contains no model code | `src/cua/replay/` imports | `tests/replay/test_replay_imports.py` |
| A payment is clicked at most once | `interpreter.py` `_before_commit`, `_after_commit`; `replay/commit_ledger.py` | `tests/replay/test_commit_path.py` |
| A lost response is never retried | `interpreter.py` `_unknown`, `retry_safe` | `test_session_expiry_after_a_commit_is_never_restarted` |
| A draft can never commit | `interpreter.py` `_preflight` | `test_draft_cannot_commit` |
| Editing a capability voids its approval | `artifact/lock.py`, `artifact/approval.py` | `tests/artifact/test_artifact_lock_approval.py` |
| What counts as a commit | `artifact/routes.py`, `replay/effects.py`, `agent/executor.py` `effect_of` | `tests/agent/test_commit_gate.py` |
| The model never sees real values or passwords | `agent/executor.py`, `surface/login.py` | `tests/agent/test_executor_tokens.py` |
| Only a person may act while they hold control | `control/lease.py` `LeaseManager` | `tests/control/test_lease.py` |
| The browser can't reach other sites | `safety/network.py` | `tests/safety/test_network.py` |
| Nothing secret reaches disk | `safety/redact.py`, `evidence/writer.py` | `tests/safety/test_redact.py`, `cua lint` |

**Good adversarial questions:**
- What if the process crashes between the click and the ledger update? (Look for the startup sweep.)
- What if the human clicks "Validate" themselves during a handoff, then resumes?
- Can a commit-word regex ever lower an effect class? Can a profile mistake hide a commit?
- What does the console do with a request from another origin, or with no Origin header? (REPORT §6.)

## Pass 6: tests as spec (20 min)

The tests double as a behaviour spec. Read these four by test name only, and open a test when its name surprises
you.

1. `tests/replay/test_replay_m4.py`: one test per failure situation.
2. `tests/replay/test_commit_path.py`: the payment path.
3. `tests/control/test_resume.py`: when a run may continue after a person.
4. `tests/safety/test_policy.py`: what is denied by default.

`docs/implementation-status.md` maps each milestone to its tests and evidence if you want to check coverage.

## What to write in your review

- **Claims checked:** which REPORT claims you traced to code and a test, and which you could not.
- **Correctness risks:** anything in Passes 4 and 5 where behaviour depends on timing, on the profile being right,
  or on an untested branch.
- **Honesty of results:** whether `result.json` statuses match `ground_truth.json`, and whether the limits in
  REPORT §7 are stated plainly.
- **Readability:** where you got lost, since that is where the next reader will too.

Known limits you don't need to rediscover (REPORT §7): one app and tenant, no frame handling, one approver,
`COMMIT_UNKNOWN` needs a human verdict, the console has no authentication, and model tests were not re-run at the
end because the API credits ran out.
