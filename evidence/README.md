# Evidence

Every bundle below was produced by a real run against the local Dolibarr target and passed through the redactor
(`src/cua/safety/redact.py`); `cua lint` re-checks all of it (`evidence_lint.txt`, test `test_committed_evidence_is_clean`).
Screenshots are masked images; they are not scanned by the lint.

Common files: `manifest.json` (hash of every file in the bundle), `events.jsonl` (ordered run log), `result.json`.

| Bundle | What it shows | Where to look |
|---|---|---|
| `discovery_lookup_customer_outstanding/` | Model-driven discovery of a read capability: 5 steps, then a draft | `transcript.json` (turns), `trace.json` (recorded steps), `capability.draft.json` (the draft as compiled on the day of the run; today's compiler adds the not-found and access-denied outcomes, so recompile the trace to get `capabilities/dolibarr/lookup_customer_outstanding@1.0.0.draft.json` byte for byte), `result.json` (token usage, and the output as the model read it from the page, before typing), `screenshots/` |
| `discovery_record_payment/` | Discovery of the write capability (payment); the model run was replayed from a transcript when API credits ran out (`model: scripted` in `result.json`) | same files, plus `interventions/` (two approvals of the commit step). Its draft is what today's compiler produces; the approved capability adds two reviewed edits (`test_approved_payment_draft_is_the_compiled_trace_plus_exactly_the_reviewed_edits`) |
| `replay_success_other_customer/` | Replay for a different customer than discovery used: success, no model | `result.json` (`outputs`, `drift: []`, the approved `lock_hash`), `ground_truth.json` (REST agrees: 355.20), `events.jsonl` |
| `replay_captcha_handoff/` | Login CAPTCHA: replay stops, a human solves it in the same session, replay continues to success | `interventions/` (the escalation request, reason `HUMAN_ONLY_LOGIN`), `failure/` (masked screenshot and snapshot at the stop), `events.jsonl` (lease changes), `ground_truth.json` |
| `replay_commit_unknown/` | Response lost after the single payment click: the run reports `COMMIT_UNKNOWN`, never re-clicks, a human gives a verdict | `result.json` (`status: indeterminate`, `side_effects`), `events.jsonl` (`commit_attempted` once), `verdicts/`, `ground_truth.json` (REST payments 0 to 1) |
| `replay_policy_denied/` | A policy that denies the payment route: replay refuses before touching the UI | `result.json` (policy denial), `events.jsonl` (no browser action) |
| `validation_suite.json` | Repeated runs of a capability's test suite: `n`, `cases`, `flakes` | the file itself; regenerate with `uv run cua validate <capability> --repeat N` |
| `validations/` | The validation runs the approvals cite (`val-m6-lookup`: 33/33, `val-m6-gate`: 5/5), each at the approved lock hash | `lock_hash` and `passed` in each file; compare with `approvals/dolibarr/*.approval.json` |
| `evidence_lint.txt` | Output of `uv run cua lint` | the file itself: `clean` or file:line:rule per finding, never the value |

## ground_truth.json

The application's own REST view, read by a side channel the replay never uses: the case inputs, the declared faults,
a hash of the whole REST state before and after the run, and the values the suite checks (for the payment: payment
count and amounts on the invoice). Compare it with `result.json` to see that what the run reported is what the
application holds. Written by `uv run cua make-evidence` (`tests/evidence/test_make_evidence.py`).

## Requirement to bundle

| Requirement | Bundle |
|---|---|
| Discover from a goal, compile an artifact | `discovery_*` |
| Replay with no model, other input | `replay_success_other_customer` |
| Human handoff in the same session | `replay_captcha_handoff` |
| Exactly-once write, unknown outcome escalated | `replay_commit_unknown` |
| Policy refusal | `replay_policy_denied` |
| Repeatable | `validation_suite.json` |
