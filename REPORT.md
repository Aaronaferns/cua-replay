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
screenshot and snapshot (`evidence/*/failure/`). `cua view` renders it as HTML (every committed bundle is rendered in
`docs/views/`); `cua export-otel` sends OTLP spans, never screenshots or page text. The error replay below lost the
response to the payment click, so the run stops `indeterminate` and hands the verdict to a person:

![Replay timeline of the lost-response error replay ending in COMMIT_UNKNOWN](docs/views/view-commit-unknown.png)

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

Every step runs through the same loop. After a navigation, the agent waits for the page to settle. The load event has
to fire, and then two polls in a row have to return the same screen (`tests/surface/test_settle_read.py`). It
classifies that screen against the profile and resolves exactly one element to act on. Before acting, it checks the
lease and the expected effect. After acting, it verifies the result: typed values have to echo back, the element's
identity has to match exactly, and the review screen gets checked before anything is committed.

If the screen isn't what the step expected, the agent tries four things in order: a branch defined on the step, the
expected state, a known handler, and finally the profile's default meaning. If none of them match, it fails closed.

Results follow the brief's classes. A business outcome returns `business_outcome` with the outcome named. A
recoverable problem is handled inside the run (a known interstitial, a bounded wait and retry, or a re-login, allowed
only before a commit) and listed under `recoveries` on whatever status results. A hard failure returns `failure` with
a code, the step, expected and observed state, and a masked screenshot; error messages never echo inputs
(`tests/safety/test_error_hygiene.py`). `indeterminate` means nobody knows whether the payment happened, so replay
doesn't retry and a person reconciles.

| Situation | Status / outcome | What happens | Where |
|---|---|---|---|
| Normal read | success | Outputs match REST | `evidence/replay_success_other_customer/` |
| Unknown customer | business_outcome `customer_not_found` | Reported as an outcome, not a failure | `not_found` |
| User without rights | business_outcome `access_denied` | The login recovery is recorded | `no_rights_user` |
| Only a near match exists | failure `IDENTITY_MISMATCH` | A near miss is never accepted | `nearest_match` |
| Known interstitial | success, recovery recorded | Dismissed, then the step is retried | `interstitial_recovered` |
| Unknown dialog | failure `UNKNOWN_DIALOG` | Fails closed | `unknown_dialog` |
| Application error page | failure `APP_ERROR` | Not retried | `app_error` |
| Slow, within or past the timeout | success / failure `TRANSIENT_EXHAUSTED` | Retries are bounded | `slow_within_timeout`, `slow_beyond_timeout` |
| Session expired | success, recovery `login` | Logs back in and restarts, but only if no commit was attempted | `session_expired`; `test_session_expiry_after_a_commit_is_never_restarted` |
| CAPTCHA at login | escalation, then success | Only a human solves it | `evidence/replay_captcha_handoff/` |
| Overpayment | business_outcome `validation_rejected` | Automation never answers Yes | `overpayment` |
| Response lost after commit | indeterminate `COMMIT_UNKNOWN` | Clicked once and never retried | `evidence/replay_commit_unknown/` |
| Policy denies the route | failure `POLICY_DENIED` | Refused before any UI action | `evidence/replay_policy_denied/` |

Bare names are live suite cases in `capabilities/dolibarr/*.tests.json`, each also unit-tested in `tests/replay/`.
Network faults are injected with browser routes; session expiry, the CAPTCHA and the no-rights user are real server
states.

**Drift.** A fallback locator that resolves is used and reported (`tests/replay/test_replay_v0.py`). Output extracts
use only the primary locator, so drift can't change where a number comes from.

**Stretch goal: confidence and approval.** Replay refuses an unapproved capability unless given `--allow-draft`.
`cua validate --repeat` scores stability: the lookup passed 33 of 33 runs with no flakes
(`evidence/validation_suite.json`), the payment 5 of 5.

## 4. Heterogeneity & multi-tenant

**Surface seam.** Capabilities and profiles hold no browser concepts
(`tests/artifact/test_artifact_schema.py::test_artifact_has_no_playwright_concepts`). Below them sits a `Surface`
protocol (`src/cua/surface/base.py`) whose observations are an outline of role, name and ref.

| Surface | Perception | Acting | Status |
|---|---|---|---|
| Web (Dolibarr) | Playwright accessibility snapshot | role-and-name locators | built |
| Legacy web, framesets | the same snapshot per frame; frame in each target's fingerprint | frame-scoped locators | designed; today a target in an empty frame is not found |
| Windows desktop | UI Automation tree in the same outline | UIA patterns (invoke, set value) | designed: a second `Surface`; nothing above it changes |
| No accessibility | screenshot, model-grounded at record time | coordinates | last resort; needs visual verification |

**Reuse across tenants.** Tenants on one vendor product differ in labels, modules and policy, not in flow, so the
profile belongs to the product and capabilities are recorded once on a reference tenant. The design, not yet built, is
a stack of overlays (product, then version, then tenant) that can override locators, state text, handlers and policy
but never steps. The lock hash covers the overlays, and approval is per tenant. I rejected re-recording per tenant.

**Drift.** Designed, not built: canary replays per tenant, a login-page version fingerprint that picks the version
layer, and quarantine for a target that drifts repeatedly.

## 5. Escalation & handoff

**Detect.** A run pauses for a person on a login CAPTCHA, an unknown commit outcome, or a commit needing approval in
discovery. Unknown screens fail closed. In discovery the stuck detector (`tests/agent/test_stuck.py`) and
`request_help` stop the run; routing them to a person is designed, not built.

**Route.** An intervention request (capability, step, reason, state, masked screenshot) goes to the bundle and the
console queue (`tests/control/test_interventions.py`, `tests/control/test_console.py`).

**Take control.** The operator works in the headed browser the run already owns. Who may act is a lease with a holder
and a generation. Claiming increments the generation, and an automated action carrying an older one is refused
(`tests/safety/test_policy.py`). I rejected a pause flag because a decision made before a handoff can land after
control has gone away and come back. The person's clicks, inputs and requests are captured with values redacted
(`tests/control/test_capture.py`).

**Hand back.** Resume returns the lease under a new generation. Replay then restarts from the entry route on the same
session, or the operator marks the run complete and replay verifies the checkpoint (`tests/replay/test_handoff.py`).
Restarting beats searching forward because after a re-login the app lands before the paused step, and steps share
screens. Resume is refused once a commit has been attempted (`tests/control/test_resume.py`).
`evidence/replay_captcha_handoff/` shows the full cycle.

## 6. Safety

Everything is denied unless a layer allows it. The risky class is the **commit**: the procedure is approved once, then
each request runs at most once. I rejected per-call confirmation, which defeats automation, and blocking writes, which
leaves nothing to automate. The profile's commit routes (method, path, query, form body) define what counts as a
commit; the capability's declared effect is a ceiling on top of that (`tests/replay/test_commit_path.py`).

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

**Disclosures.** API credits ran out during the payment discovery, so I finished it by replaying the model's recorded
choices (`evidence/discovery_record_payment/result.json`: `model: scripted`). The model tests were not re-run at the
end. The clean-clone run was verified on Windows, not Linux.

**Known issue.** The approved `record_invoice_payment@1.0.0` still has "50 Euros" in one of the commit button's
fallback finders and in its fingerprint. If the primary finder ever missed, the commit would fail closed for any other
amount. The compiler is fixed, and 1.0.0 is waiting for re-approval
(`test_approved_payment_draft_is_the_compiled_trace_plus_exactly_the_reviewed_edits`).
