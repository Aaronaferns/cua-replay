# REPORT

Behaviour claims cite a test (`tests/...`) or evidence bundle (`evidence/...`); commands are in `README.md`.

## 1. Architecture

`cua discover` (a model drives the live app; every action recorded) → `cua compile` (typed capability) →
`cua validate`, `cua approve` (live suite; a different approver signs the hash) → `cua replay` (no model). One Python
process: Playwright drives Chromium and the FastAPI operator console shares its event loop, so both act on the same
live page. Replay imports neither agent nor compiler (`tests/replay/test_replay_imports.py`). Diagram:
`docs/architecture.svg`.

Discovery is one loop: observe the main content's accessibility outline (text; screenshots optional), ask the
model for one closed-vocabulary action, check policy, act, record. Inputs reach the model only as `{{name}}` tokens.
It stops on `done`, 30 turns or 7 minutes (defaults), the stuck detector, a model refusal or error, or `request_help`.

| Decision | Why | Rejected |
|---|---|---|
| No model in replay | Cost, latency, determinism; production data never reaches a model | One-step model fallback: breaks the data boundary |
| Perceive via the accessibility snapshot | One engine to observe, act, record, replay; no test IDs; desktops have the same tree | Screenshot + coordinates: brittle replay |
| Own closed action vocabulary, one model adapter | It is the artifact's language; banks choose their model vendor | Vendor toolset: lock-in |
| No agent framework | A workflow checkpoint can't hold the live browser a handoff needs | LangGraph, BrowserGym |
| Python + Playwright | Playwright parity; desktop and vision tooling is Python-native | TypeScript |
| Dolibarr ERP as the bank proxy | Invoice payment is shaped like a posting (search, record, confirm, irreversible write); legacy markup I didn't write; REST as an independent oracle | Self-built app (grading my own exam); public demo (can't reset) |

Model: `claude-opus-5-5` via the Anthropic SDK; a spike completed the lookup in all 4 runs
(`docs/implementation-status.md`).

**Evidence.** Each run writes a bundle: `events.jsonl` (model actions with stated rationale, replay steps, policy
decisions, lease changes, human actions), redacted transcript and trace, `result.json`, and on failure a masked
screenshot and snapshot (`evidence/*/failure/`). `cua view` renders it as HTML; `cua export-otel` sends OTLP spans,
never screenshots or page text.

## 2. Artifact schema

A capability is a guarded linear program over classified screen states
(`capabilities/dolibarr/lookup_customer_outstanding@1.0.0.json`, trimmed):

```json
"contract": {
  "inputs":   {"customer_code": {"type": "string", "pattern": "CU\\d{4}-\\d{4,5}", "sensitivity": "pii"}},
  "outputs":  {"outstanding_amount": {"type": "decimal", "sensitivity": "internal"}},
  "outcomes": {"customer_not_found": {"returns": []}, ...}
},
"steps": [{"id": "s01_navigate", "expect": "home", "effect": "read", "timeout_ms": 10000,
           "do": {"kind": "navigate", "route": "/societe/list.php?search_customer_code=${customer_code}"}}, ...],
"success": {"state": "customer_tab", "outputs": ["outstanding_amount"], "checks": [...]},
"targets": {"cell_customer_code": {
  "strategies": [{"kind": "same_row", "label": "Customer Code", "take": "value",
                  "why": "the row's label cell leads to it", "matched_at_record": "1 of 1, same element as ref"}],
  "fingerprint": {"role": "cell", "tag": "td", "row_label": "Customer Code"}}},
"provenance": {"trace_run_id": "...", "trace_sha256": "...", "discovered_by": "claude-opus-5-5", "compiler": "..."}
```

Steps also carry an intent, optional branches (`on`) and an optional verify predicate. Screen states live in a
per-app profile (`apps/dolibarr/profile.json`: signatures, interstitial handlers, login flow, commit routes, default
meanings).

Why this shape. A click list can't react to a different screen; a full state machine is hard to review. One run never
shows error screens, so states come from the profile. Locators rank role and name, text, then structure, never
generated IDs, and must match exactly one element by normalized-exact name: the real risk is unique-but-wrong (six
Dolibarr filter inputs share one name, `docs/architecture-review-v2.md`). A fallback counts only if its fingerprint
matches.

Versioning: semver plus a lock hash over capability and profile, since both change what replay does. Approval is a
sidecar (`approvals/`), so approving never changes hashed content. Humans read the review card (`*.review.md`), agents
the contract.

## 3. Determinism & error handling

Each step: after a navigation, settle (load event, then an identical screen on two polls;
`tests/surface/test_settle_read.py`); classify against the profile; resolve exactly one element; check lease and
effect; act; verify (typed values echoed, identity exact, review screen checked before a commit). An unexpected screen
is matched in order: step branch, expected state, known handler, profile default meaning; anything else fails closed.

The brief's classes: business outcome → `business_outcome` with a named outcome. Recoverable → handled in the run
(known interstitial, bounded wait and retry, re-login before any commit), listed under `recoveries` on whatever status
results. Hard failure → `failure` with code, step, expected and observed state
and a masked screenshot; errors never echo inputs (`tests/safety/test_error_hygiene.py`). `indeterminate` means "we
don't know whether the payment happened": don't retry, reconcile.

| Situation | Status / outcome | Behaviour | Where |
|---|---|---|---|
| Normal read | success | outputs equal REST | `evidence/replay_success_other_customer/` |
| Unknown customer | business_outcome `customer_not_found` | not a failure | `not_found` |
| User without rights | business_outcome `access_denied` | login recovery recorded | `no_rights_user` |
| Nearest match only | failure `IDENTITY_MISMATCH` | never accepts a near miss | `nearest_match` |
| Known interstitial | success, recovery recorded | dismissed, step retried | `interstitial_recovered` |
| Unknown dialog | failure `UNKNOWN_DIALOG` | fails closed | `unknown_dialog` |
| Application error page | failure `APP_ERROR` | not retried | `app_error` |
| Slow within / beyond timeout | success / failure `TRANSIENT_EXHAUSTED` | bounded retries | `slow_within_timeout`, `slow_beyond_timeout` |
| Session expired | success, recovery `login` | re-login and restart only if no commit was attempted | `session_expired`; `test_session_expiry_after_a_commit_is_never_restarted` |
| Login CAPTCHA | escalation, then success | human only | `evidence/replay_captcha_handoff/` |
| Overpayment | business_outcome `validation_rejected` | automation never answers Yes | `overpayment` |
| Response lost after commit | indeterminate `COMMIT_UNKNOWN` | clicked once, never retried | `evidence/replay_commit_unknown/` |
| Policy denies the route | failure `POLICY_DENIED` | refused before any UI action | `evidence/replay_policy_denied/` |

Bare names are live suite cases (`capabilities/dolibarr/*.tests.json`), each also unit-tested in
`tests/replay/`. Network faults are browser routes; session expiry, CAPTCHA and no-rights are real server states.
Drift: a resolving fallback locator is used and reported (`tests/replay/test_replay_v0.py`); output extracts use only
the primary locator, so drift can't change where a number comes from.

Stretch goal built: confidence and approval. Replay refuses unapproved capabilities without `--allow-draft`;
`cua validate --repeat` scores stability: lookup 33/33, no flakes (`evidence/validation_suite.json`); payment 5/5.

## 4. Heterogeneity & multi-tenant

**Surface seam.** Capabilities and profiles hold no browser concepts
(`tests/artifact/test_artifact_schema.py::test_artifact_has_no_playwright_concepts`). Below them is a `Surface`
protocol (`src/cua/surface/base.py`) whose observations are a "role, name, ref" outline.

| Surface | Perception | Acting | Status |
|---|---|---|---|
| Web (Dolibarr) | Playwright accessibility snapshot | role-and-name locators | built |
| Legacy web, framesets | the same snapshot per frame; frame in each target's fingerprint | frame-scoped locators | designed; today a target in an empty frame is not found |
| Windows desktop | UI Automation tree in the same outline | UIA patterns (invoke, set value) | designed: a second `Surface`; nothing above it changes |
| No accessibility | screenshot, model-grounded at record time | coordinates | last resort; needs visual verification |

**Reuse across tenants.** Tenants on one vendor product differ in labels, modules and policy, not flow, so the profile
belongs to the product and capabilities are recorded once on a reference tenant. Designed: overlays (product → version
→ tenant) override locators, state text, handlers and policy, never steps; the lock hash covers them, and approval is
per tenant. Re-recording per tenant was rejected.

**Drift.** Designed: per-tenant canary replays, a login-page version fingerprint to pick the version layer, and
quarantine after repeated drift on one target.

## 5. Escalation & handoff

**Detect.** A run pauses for a person on a login CAPTCHA, an unknown commit outcome, or a commit needing approval in
discovery. Unknown screens fail closed. In discovery the stuck detector (`tests/agent/test_stuck.py`) and
`request_help` stop the run; routing them to a person is designed, not built.

**Route.** An intervention request (capability, step, reason, state, masked screenshot) is written to the bundle
and the console queue (`tests/control/test_interventions.py`, `tests/control/test_console.py`).

**Take control.** The operator works in the headed browser the run already owns. Who may act is a lease with a holder
and a generation; claiming increments the generation, and an automated action with an older one is refused
(`tests/safety/test_policy.py`). A pause flag was rejected: a decision made before a handoff can land after control
went away and back. The person's clicks, inputs and requests are captured, values redacted
(`tests/control/test_capture.py`).

**Hand back.** Resume returns the lease with a new generation; replay restarts from the entry route on the same
session, or the operator marks the run complete and replay verifies the checkpoint (`tests/replay/test_handoff.py`).
Restarting beats searching forward: after a re-login the app lands before the paused step, and steps share screens.
Resume is refused once a commit was attempted (`tests/control/test_resume.py`). `evidence/replay_captcha_handoff/`
shows the full cycle.

## 6. Safety

Everything is denied unless a layer allows it. The risky class is the **commit**: the procedure is approved once, then
each request runs at most once. I rejected per-call confirmation, which defeats automation, and blocking writes, which
leaves nothing to automate. Commits are defined by the profile's commit routes (method, path, query,
form body); the capability's declared effect is a ceiling (`tests/replay/test_commit_path.py`).

| Control | Enforced by | Shown in | Limit |
|---|---|---|---|
| Network allow-list | browser routing blocks other origins, redirects, websockets, downloads, service workers | `tests/safety/test_network.py` | one host and port per sandbox policy |
| Action policy | policy file (`policies/*.json`): effect ceiling checked on every action and in a static preflight | `tests/safety/test_policy.py`, `evidence/replay_policy_denied/` | per policy, not per user |
| Second-person approval | lock hash over capability and profile; approver name must differ from `discovered_by` | `tests/artifact/test_artifact_lock_approval.py` | names are self-declared labels; here the author approved both; no dual control |
| Commit at most once | request-ID ledger; intent logged before the click; no retry after an unknown outcome | `tests/replay/test_commit_path.py`, `evidence/replay_commit_unknown/` | a lost response needs a human verdict |
| Commit approval in discovery | single-use console approval bound to action and lease generation | `tests/control/test_approver.py`, `tests/agent/test_commit_gate.py` | console only |
| Secrets and personal data | scripted login (the model never sees credentials); one redacting writer; masked screenshots; `cua lint` | `tests/safety/test_redact.py`, `evidence/evidence_lint.txt` | lint reads text; screenshots are masked, not scanned |
| Human control | lease holder and generation (§5) | `tests/control/test_lease.py` | console loopback-only and unauthenticated (single-user machine only); input before a claim is accepted, uncaptured |

Discovery sends page text to a model, so it runs on sandbox data.

## 7. Cuts

Cut, in the order I'd build next:

- A second tenant and the overlays (§4).
- Differential read-back to settle `COMMIT_UNKNOWN` automatically.
- Routing unknown screens and stuck discovery to a person (§5).
- Capturing or blocking input before a claim.
- A desktop surface; frames, popups and pixel surfaces.
- An authenticated, remote operator console.
- Also cut: a second model provider and the model comparison, dual control above a threshold, test-payment cleanup.

Disclosed: API credits ran out during the payment discovery, finished by replaying the model's recorded choices
(`evidence/discovery_record_payment/result.json`: `model: scripted`); model tests were not re-run at the end. The
clean-clone run was verified on Windows, not Linux.

Known issue: approved `record_invoice_payment@1.0.0` keeps "50 Euros" in one of the commit button's fallback finders
and in its fingerprint; if its primary finder ever missed, the commit would fail closed for other amounts. The
compiler is fixed; 1.0.0 awaits re-approval (`test_approved_payment_draft_is_the_compiled_trace_plus_exactly_the_reviewed_edits`).
