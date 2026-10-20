# REPORT

A model learns a task once; a person approves what it learned; a program with no model in it repeats the task.
Every claim below names a test (`tests/...`) or an evidence bundle (`evidence/...`). Commands are in `README.md`.

## 1. Architecture

`cua discover` (a model drives the live app; every action is recorded) → `cua compile` (trace to typed capability)
→ `cua validate` and `cua approve` (live test suite, then a second person signs the hash) → `cua replay` (no model). One
Python process: Playwright drives Chromium and the FastAPI operator console shares its event loop, so both act on the
same live page.

| Decision | Why | Rejected alternative |
|---|---|---|
| No model in replay (`tests/replay/test_replay_imports.py`) | Cost, latency and determinism; production data never reaches a model | A bounded model fallback for one failed step: it reopens the data boundary and what approval means |
| Perception = the accessibility snapshot, scoped to the main content | One engine to observe, act, record and replay; needs no test IDs; desktops have the same kind of tree | Screenshot + coordinates: records poorly and replays brittlely |
| Own closed action vocabulary, one model adapter | The model's vocabulary is the artifact's language; banks choose their model vendor | A vendor browser toolset: a second vocabulary and lock-in |
| Dolibarr, a real open-source ERP, as target; faults injected only at the network layer, declared per test case | Legacy markup I didn't write, real errors and permissions, a REST API as an independent oracle, reset in ~9 s | A self-built hostile app (grading my own exam); a public demo (can't reset or verify) |

The real discovery run (`evidence/discovery_lookup_customer_outstanding/`, `claude-opus-5-5`) compiles to a
capability that replays for another customer with no model; REST agrees (`evidence/replay_success_other_customer/`).

## 2. Artifact schema

A capability (`capabilities/dolibarr/lookup_customer_outstanding@1.0.0.json`) is a guarded linear program over
classified screen states:

- **contract:** typed inputs (with pattern and sensitivity), typed outputs, and the business outcomes a caller can get;
- **steps:** intent, the screen state expected before acting, one action from a closed set, an effect class
  (none, read, commit), optional branches to outcomes, and a verify predicate;
- **targets:** ranked locator strategies, each with a `why` and its match count at record time, plus a fingerprint;
- **success:** the final state, the outputs to return, and visible-evidence checks;
- **provenance:** the trace's hash, the model, the compiler version.

Screen states live in a separate **app profile** (`apps/dolibarr/profile.json`): signatures, specializations,
interstitial handlers, login flow, commit routes, default meanings. It is authored once per application.

Why this shape. A click list can't react to a different screen; a full state machine is hard to review. One recorded
run never shows error screens, so states come from the profile, not the trace. Locators prefer role and accessible name,
then text, then structure, never generated IDs, and must match exactly one element, names compared normalized-exact:
the real risk is unique-but-wrong (Dolibarr has six filter inputs with one name, `spikes/s7_name_match.py`). A fallback
counts only if its fingerprint matches. Inputs reach the page only as typed tokens (`tests/agent/test_executor_tokens.py`).

Versioning: semver plus a lock hash over capability **and** profile, since both change what replay does; approval is
a sidecar (`approvals/`), so approving never changes the hashed content. Humans read the review card (`*.review.md`),
agents the contract. The lookup draft recompiles byte for byte; the payment draft is the compiled trace plus two
reviewed edits, pinned exactly (`tests/compiler/test_committed_artifact.py`).

## 3. Determinism & error handling

Each step: settle (load event, then an identical screen on two polls, `tests/replay/test_settle_fence.py`); classify
against the profile; resolve exactly one element; check lease and effect; act; verify (typed values echoed, identity
matched exactly, review screen checked before a commit); wait for the next expected state. An unexpected screen is
matched in fixed order: the step's branch, the expected state, a known handler, the profile's default meaning; anything
else fails closed.

Four statuses: **success**, **business outcome**, **failure**, **indeterminate**. The fourth exists because "we don't
know whether the payment happened" gives the caller a different duty (don't retry; reconcile). Recoveries and locator
drift are reported on every status.

| Situation | Status / outcome | Behaviour | Where |
|---|---|---|---|
| Normal read | success | outputs equal REST | `evidence/replay_success_other_customer/` |
| Unknown customer | business_outcome `customer_not_found` | not a failure | `test_unknown_code_is_customer_not_found` |
| User without rights | business_outcome `access_denied` | login recovery recorded | `test_access_denied_is_a_business_outcome` |
| Nearest match only | failure `IDENTITY_MISMATCH` | never accepts a near miss | `test_near_miss_is_identity_mismatch` |
| Known interstitial | success, recovery recorded | dismissed, step retried | `test_known_interstitial_is_dismissed_and_recorded` |
| Unknown dialog | failure | stops, fails closed | `test_unknown_dialog_fails_closed` |
| Application error page | failure `APP_ERROR` | classified, not retried | `test_app_error_page_is_an_app_failure` |
| Slow within / beyond timeout | success / failure `TRANSIENT_EXHAUSTED` | bounded retries | `test_slow_within_timeout_not_failure`, `test_slow_beyond_timeout_transient_exhausted` |
| Session expired | success, recovery `login` | re-login and restart only if no commit was attempted | `test_session_expiry_after_a_commit_is_never_restarted` |
| Login CAPTCHA | escalation, then success | human only | `evidence/replay_captcha_handoff/` |
| Overpayment | business_outcome `validation_rejected` | automation never answers Yes | `test_overpayment_is_business_outcome` |
| Response lost after commit | indeterminate `COMMIT_UNKNOWN` | clicked once, never retried | `evidence/replay_commit_unknown/` |
| Policy denies the route | failure `POLICY_DENIED` | refused before any UI action | `evidence/replay_policy_denied/` |

Unqualified tests are in `tests/replay/test_replay_m4.py` and `test_commit_path.py`. A failure carries code, step,
expected and observed state, and a masked URL, screenshot and snapshot; errors never echo inputs
(`tests/safety/test_error_hygiene.py`). UI drift surfaces when a fallback locator resolves: replay continues and reports
it (`test_fallback_strategy_is_used_and_reported_as_drift`).

`cua validate` runs each capability's suite live, resetting before every case and checking results against REST:
lookup 33/33 over three repeats, no flakes (`evidence/validation_suite.json`); payment 5/5. Approvals cite these runs
(`evidence/validations/`).

## 4. Heterogeneity & multi-tenant

Built: one surface (web), one application, one tenant. The abstractions are drawn so the rest does not need a rewrite.

**Surface seam.** Capabilities and profiles contain no browser concepts (`test_artifact_has_no_playwright_concepts`).
Everything below them goes through a `Surface` protocol (`src/cua/surface/base.py`: observe, settle, find, read_text,
click, type, select, navigate, fingerprint). Observations are a "role, name, ref" outline other surfaces can produce.

| Surface | Perception | Acting | Status |
|---|---|---|---|
| Web (Dolibarr) | Playwright accessibility snapshot | role-and-name locators | built |
| Legacy web with framesets | the same snapshot per frame; the frame recorded in each target's fingerprint | frame-scoped locators | designed; today an empty frame is reported `frames_unavailable` and fails closed |
| Windows desktop | the UI Automation tree, rendered into the same outline | UI Automation patterns (invoke, set value) | designed: a second `Surface`; capability, profile and replay unchanged |
| No accessibility at all | screenshot, grounded by a model at record time | coordinates | last resort; replay would need visual verification |

**Reuse across tenants.** Tenants running the same vendor product differ mostly in labels, enabled modules and
policy, not in flow. So the profile belongs to the product, and capabilities are recorded once on a reference tenant.
The design adds layered overlays (product → version → tenant) that may override locator strategies, state-signature
text, enabled handlers and the tenant's policy, but never the steps. The lock hash then covers capability, profile and
overlay, and approval is per tenant. Re-recording per tenant was rejected: hundreds of tenants times twenty apps.

**Detecting drift.** Built: every fallback use is reported in `drift`. Designed: scheduled canary replays of read
capabilities per tenant; a version fingerprint (the app's version string on its login page) to select the version
layer; and repeated drift on one target quarantines that capability for that tenant until the affected steps are
re-discovered.

## 5. Escalation & handoff

**Detect.** A run stops for a person on a login CAPTCHA, a screen the profile does not know, an unknown commit
outcome, a commit that needs approval during discovery, or, in discovery, the stuck detector (the same screen,
including field values, several times without progress; `tests/agent/test_stuck.py`).

**Route.** It writes an intervention request (capability, step, reason, classified and expected state, masked
screenshot) to the console queue and pauses; only unclaimed requests expire (`test_sla_only_for_unclaimed`).

**Take control.** The operator surface is the headed browser the run already owns, so the person works in the same
live session; real-time co-browsing is out of scope. Who may act is a lease with a holder and a generation number.
Claiming moves it to the person and increments the generation, and any automated action carrying an older generation
is refused (`test_stale_lease_generation_cannot_act`). A boolean pause flag was rejected: a model call takes seconds, so
a decision made before a handoff can land after control went to the person and back. The person's clicks, inputs and
requests are captured with values redacted (`tests/control/test_capture.py`).

**Hand back.** Resume returns the lease to automation with a new generation; replay restarts from the entry route on
the same session, or the operator marks the run complete and replay verifies the success checkpoint
(`tests/replay/test_handoff.py`). Restarting beats searching forward: after a re-login the app lands before the paused
step, and steps share screens. Resume is refused once any commit was attempted (`tests/control/test_resume.py`).
`evidence/replay_captcha_handoff/` shows the whole cycle: escalation, claim, seven captured human actions, resume, and
a result equal to REST.

Limit: the browser accepts input before anyone claims the request, and such input is not captured. Next: capture
whenever automation does not hold the lease, or block input until a claim.

## 6. Safety

Deny by default, in layers. The risky class is the **commit**: the procedure is approved once, then each request runs
at most once. Per-call confirmation defeats automation; blocking writes makes the system useless. A commit is defined
by the profile's commit routes (method, path, query and form body); a commit-word pattern may only raise a step's
class, and the capability's declared effect is a ceiling (`tests/agent/test_commit_gate.py`).

| Control | Enforced by | Shown in | Limit |
|---|---|---|---|
| Network allow-list | browser-layer routing blocks other origins, redirects, websockets, downloads, service workers | `tests/safety/test_network.py` | one origin per sandbox policy |
| Action policy | policy file with an effect ceiling, checked on every action and in a static preflight | `tests/safety/test_policy.py`, `evidence/replay_policy_denied/` | per policy, not per user |
| Second-person approval | lock hash over capability and profile; approver differs from discoverer | `tests/artifact/test_artifact_lock_approval.py` | one approver, no dual control |
| Commit at most once | request-ID ledger; intent logged before the click; no retry after an unknown outcome | `tests/replay/test_commit_path.py`, `evidence/replay_commit_unknown/` | a lost response needs a human verdict |
| Commit approval in discovery | single-use console approval bound to the action and lease generation | `tests/control/test_approver.py` | console approval only |
| Secrets and personal data | scripted login, so the model never sees credentials; one redacting evidence writer; masked screenshots; `cua lint` | `tests/safety/test_redact.py`, `evidence/evidence_lint.txt` | lint reads text; screenshots are masked, not scanned |
| Human control | lease holder and generation (§5) | `tests/control/test_lease.py` | the console has no authentication |

The console binds to loopback only, checks the Host header and the Origin of browser requests, and accepts only JSON
bodies. It has no authentication, and requests without an Origin header (non-browser clients) skip the Origin check;
both are acceptable only on a single-user machine. Discovery sends page text to a model, so it runs on sandbox data.

## 7. Cuts

Deliberately left out:

- A second tenant and the overlay layers (§4): designed, not built.
- Frames, popups, desktop and pixel surfaces (§4).
- A second model provider: one adapter behind the `ModelAdapter` interface.
- Differential read-back, which would let the system settle `COMMIT_UNKNOWN` itself; dual control above a threshold;
  automatic cleanup of test payments.
- An authenticated, remote operator console.

Disclosed: the API credits ran out during the payment discovery, which was finished by replaying the model's recorded
choices (`evidence/discovery_record_payment/result.json`: `model: scripted`), and model tests were not re-run at the
end. The clean-clone run was verified on Windows; Linux was not.

Known issue: in the approved `record_invoice_payment@1.0.0`, the commit button `button_validate` carries the recorded
amount in one fallback finder and in its fingerprint's row label (`"Do you confirm this payment input for 50 Euros?
..."`). The compiler counted an amount as data only with two decimals, so "50 Euros" passed. Replay is unaffected
while the primary finder (`role button "Validate"`) matches. If it ever did not, every fallback would be rejected for
any other amount, because a fallback's hit must match the fingerprint: the commit button fails closed, which is safe
but was not designed. The compiler now also treats a whole number next to a currency as data, so new compiles leave
both out; 1.0.0 stays as approved until a re-approval, and
`test_approved_payment_draft_is_the_compiled_trace_plus_exactly_the_reviewed_edits` names it as the one allowed
difference. Two gaps remain: discovery drops finders that contain an extracted value but not a typed input value in
another format (typed "50.00", shown "50"), and static validation does not check amounts in finders.

Next, in order: the tenant overlay with a second tenant, differential read-back for unknown commits, capturing or
blocking input before a claim, a desktop surface, then console authentication.
