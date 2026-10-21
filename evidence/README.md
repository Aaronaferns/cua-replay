# Evidence

Every run bundle below (`discovery_*`, `replay_*`) was produced by a real run against the local Dolibarr target and
passed through the redactor (`src/cua/safety/redact.py`); `cua lint` re-checks the whole folder (`evidence_lint.txt`,
test `test_committed_evidence_is_clean`). Screenshots are masked images; they are not scanned by the lint.

Common files: `manifest.json` (hash of every file in the bundle), `events.jsonl` (ordered run log), `result.json`.
To read a bundle as an HTML timeline instead of raw JSON, run `uv run cua view evidence/ --open` (README, "Evidence
and observability"); it reads the bundles and changes nothing.

| Bundle | What it shows | Where to look |
|---|---|---|
| `discovery_lookup_customer_outstanding/` | Model-driven discovery of a read capability: 5 steps, then a draft | `transcript.json` (turns), `trace.json` (recorded steps), `capability.draft.json` (see below), `result.json` (token usage, and the output as the model read it from the page, before it is parsed into a decimal), `screenshots/` |
| `discovery_record_payment/` | Discovery of the write capability (payment); the model run was replayed from a transcript when API credits ran out (`model: scripted` in `result.json`) | same files, plus `interventions/` (two operator approvals: one for submitting the payment form, one for the Yes on Dolibarr's confirmation) |
| `replay_success_other_customer/` | Replay for a different customer than discovery used: success, no model | `result.json` (`outputs`, `drift: []`, the approved `lock_hash`), `ground_truth.json` (REST agrees: 355.20), `events.jsonl` |
| `replay_captcha_handoff/` | Login CAPTCHA: replay stops, a human solves it in the same session, replay continues to success | `interventions/` (the escalation request, reason `HUMAN_ONLY_LOGIN`), `failure/` (masked screenshot and snapshot at the stop), `events.jsonl` (lease changes), `ground_truth.json` |
| `replay_commit_unknown/` | Response lost after the single payment click: the run reports `COMMIT_UNKNOWN`, never re-clicks, a human gives a verdict | `result.json` (`status: indeterminate`, `side_effects`), `events.jsonl` (`commit_attempted` once), `verdicts/`, `ground_truth.json` (REST payments 0 to 1) |
| `replay_policy_denied/` | A policy that denies the payment route: replay refuses before touching the UI | `result.json` (`POLICY_DENIED`, `step_id: null`), `events.jsonl` (session opened, two policy denials, the result; no browser action), `failure/`, `ground_truth.json` (REST unchanged) |
| `validation_suite.json` | Repeated runs of a capability's test suite: `n`, `cases`, `flakes` | the file itself; regenerate with `uv run cua validate dolibarr.lookup_customer_outstanding --repeat 3` (written only when `--repeat` is 2 or more) |
| `validations/` | The validation runs the approvals cite (`val-m6-lookup`: 33/33, `val-m6-gate`: 5/5), each at the approved lock hash | `lock_hash` and `passed` in each file; compare with `approvals/dolibarr/*.approval.json` |
| `evidence_lint.txt` | Output of `uv run cua lint` | the file itself: `clean` or file:line:rule per finding, never the value |

## The two discovery drafts

Each `capability.draft.json` is what the compiler produced on the day of its run.

For the lookup, today's compiler adds the not-found and access-denied outcomes and drops one fingerprint row label
that contained an amount. Recompiling `trace.json` today gives the committed lookup draft in `capabilities/dolibarr/`
byte for byte (`test_compile_from_trace_reproduces_draft`).

The payment draft was compiled before commit c671482, which made a whole-number amount next to a currency count as
data. Its targets match the committed payment draft in `capabilities/dolibarr/`, and the approved capability is that
draft plus two reviewed edits (`test_approved_payment_draft_is_the_compiled_trace_plus_exactly_the_reviewed_edits`).
Recompiling the trace today differs only at the Validate button: its first fallback finder, which keys on the
confirmation prompt ("... for 50 Euros? ..."), and the fingerprint row label holding the same prompt are both left out.
Approved 1.0.0 still has both until it is re-approved (REPORT §7).

## ground_truth.json

The application's own REST view, read by a side channel the replay never uses: the case inputs, the declared faults,
a hash of the whole REST state before and after the run, and the values the suite checks (for the payment: payment
count and amounts on the invoice). Compare it with `result.json` to see that what the run reported is what the
application holds. Written by `uv run cua make-evidence` (`tests/evidence/test_make_evidence.py`).
