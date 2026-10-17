# REPORT

Claims name a test (`tests/...`) or an evidence bundle (`evidence/...`). Commands are in `README.md`.

## 1. Architecture

A model explores the application once; replay never involves a model. Discovery (`cua discover`) records a trace and
compiles it into a capability draft: ordered steps, each with locator strategies, an expected state and a proposed
effect level (read or commit). A person reviews the review card and approves the draft's lock hash with `cua approve`;
a draft cannot commit (`tests/replay/test_commit_path.py::test_draft_cannot_commit`) and approval is bound to the hash
(`tests/artifact/test_artifact_lock_approval.py`). Replay (`cua replay`) resolves each step in a browser, classifies the
page it lands on, and returns a result with status, outcome, outputs, recoveries and drift.

Why Dolibarr: a real app; we inject only network-level faults and declare them. A fault is a rule in the browser's
network layer (slow response, error page, interstitial, unknown dialog, response lost after a commit), declared in the
suite (`capabilities/dolibarr/*.tests.json`) and echoed in each bundle's `ground_truth.json`. Replay cannot import the
fault code (`tests/replay/test_replay_m4.py::test_replay_never_imports_faults`) or any model code
(`tests/replay/test_replay_imports.py::test_replay_never_imports_model_code`).

Evidence: `evidence/discovery_lookup_customer_outstanding/` is a real model run (5 steps); replaying the compiled
capability for another customer gives the same fields with no model (`evidence/replay_success_other_customer/`,
`tests/replay/test_live_g1.py`).

## 2. Artifact schema

A capability file holds inputs, outputs, steps and a lock hash over everything that affects behaviour. Locators are
semantic (role and name, text, then structure), not generated ids. Inputs reach the page only as typed tokens, never
raw values (`tests/agent/test_executor_tokens.py::test_tokens_reject_raw_values_all_args`). The lookup draft is
reproducible from its trace byte for byte (`tests/compiler/test_committed_artifact.py::test_compile_from_trace_reproduces_draft`);
the payment draft is the compiled trace plus two reviewed edits, an explicit not-found branch and bank-account checks
that read the dropdown rather than its wrapping cell, and a test fails on any other difference
(`::test_approved_payment_draft_is_the_compiled_trace_plus_exactly_the_reviewed_edits`). The schema is
frozen (`tests/artifact/test_schema_freeze.py`), and a change to the file changes the lock hash and voids the approval
(`tests/artifact/test_artifact_lock_approval.py`). Example: `capabilities/dolibarr/lookup_customer_outstanding@1.0.0.json`
with its review card `...review.md`. Suites are strict and name their cases, expected statuses and ground-truth
oracles (`tests/replay/test_validate.py::test_suite_schema_is_strict_and_unique`).

## 3. Determinism & error handling

`cua validate <capability> --repeat N` resets the target before every case and compares status, outcome, outputs,
recoveries and drift, and, where the suite names one, the application's own REST value
(`tests/replay/test_validate.py::test_validate_repeat_runs_the_whole_suite_n_times`,
`::test_ground_truth_disagreement_fails_the_case`). `evidence/validation_suite.json` records n, cases and flakes.

The taxonomy below covers the lookup and payment suites. "Where" is the test that pins the row or the bundle that
shows it; unqualified test names are in `tests/replay/test_replay_m4.py` or `tests/replay/test_commit_path.py`.

| Situation | Status / outcome | Behaviour | Where |
|---|---|---|---|
| Normal read | success | outputs equal REST | `evidence/replay_success_other_customer/` |
| Unknown customer | business_outcome `customer_not_found` | not a failure | `test_unknown_code_is_customer_not_found` |
| User without rights | business_outcome `access_denied` | login recovery recorded | `test_access_denied_is_a_business_outcome` |
| Nearest match only | failure `IDENTITY_MISMATCH` | never accepts a near miss, returns no value | `test_near_miss_is_identity_mismatch` |
| Known interstitial | success, recovery recorded | dismissed, step retried | `test_known_interstitial_is_dismissed_and_recorded` |
| Unknown dialog | failure | stops, fail closed | `test_unknown_dialog_fails_closed` |
| Application error page | failure | classified as application failure | `test_app_error_page_is_an_app_failure` |
| Slow within timeout | success | not a failure | `test_slow_within_timeout_not_failure` |
| Slow beyond timeout | failure | transient, retries exhausted | `test_slow_beyond_timeout_transient_exhausted` |
| Session expired | success, recovery `login` | re-login and restart only if retry-safe; never after a commit | `test_session_expiry_relogs_in_and_restarts_when_retry_safe`, `test_session_expiry_after_a_commit_is_never_restarted` |
| Login CAPTCHA | escalation | human-only, never filled | `evidence/replay_captcha_handoff/` |
| Commit confirmed | success, one payment | exactly one payment | `test_confirmed_commit_is_a_success_with_one_payment` |
| Overpayment | business_outcome | automation never answers Yes | `test_overpayment_is_business_outcome` |
| Response lost after commit | indeterminate `COMMIT_UNKNOWN` | clicked once, never retried | `evidence/replay_commit_unknown/` |
| Policy denies the route | refusal before any UI action | UI untouched | `evidence/replay_policy_denied/` |

A failure carries code, step, expected and observed state and a masked URL
(`test_failure_carries_expected_observed_and_masked_url`), plus a masked screenshot and snapshot
(`test_failure_evidence_is_captured_through_the_evidence_writer`). Error text never echoes input values
(`tests/safety/test_error_hygiene.py`).

## 4. Heterogeneity & multi-tenant

One application, one tenant. What generalises is the profile (`apps/dolibarr/profile.json`: login flow, commit routes,
state markers) kept apart from the capability, and role-and-name locators with content predicates instead of ids; the
lookup template is URL-only because Dolibarr's main content has no heading role (`docs/implementation-status.md`, M3).
Not shown: a second application or tenant, and framesets. Dolibarr has no frames, so the surface reads the main frame
only (`src/cua/surface/web.py`) and flags an empty frame as `frames_unavailable`; no test or bundle exercises frame
content. Per-version generated ids are likewise absent. A second tenant was cut (§7).

## 5. Escalation & handoff

When replay meets something only a person may do, it writes an intervention record and pauses.
`evidence/replay_captcha_handoff/` shows the real case: the login CAPTCHA (`interventions/`, reason `HUMAN_ONLY_LOGIN`);
a person solves it in the same browser session through the operator console; replay then continues to a result equal
to REST (`ground_truth.json`). Control is a lease with a generation number: automation cannot act while a person holds
it, and a stale generation is refused (`tests/control/test_lease.py`,
`tests/safety/test_policy.py::test_stale_lease_generation_cannot_act`). Resume is refused after any commit attempt or
human commit click (`tests/control/test_resume.py`); `tests/replay/test_handoff.py` covers resume and mark-complete.
For an unknown commit outcome, `evidence/replay_commit_unknown/` holds the escalation, the operator verdict
(`verdicts/`) and REST showing one payment where there was none (`ground_truth.json`);
`test_unknown_commit_escalates_and_records_the_verdict` pins it. An unanswered request expires only while unclaimed
(`tests/control/test_lease.py::test_sla_only_for_unclaimed`).

## 6. Safety

| Control | Enforced by | Shown in | Limit |
|---|---|---|---|
| Network allow-list | browser-layer guard blocks off-allowlist requests, redirects, websockets, downloads | `tests/safety/test_network.py` | one origin per sandbox policy |
| Action policy, deny by default | `policies/dolibarr-sandbox.json`, effect ceiling | `tests/safety/test_policy.py::test_policy_denies_by_default`, `::test_guard_enforces_the_effect_ceiling` | ceiling is per policy, not per user |
| Approval of the lock hash | `cua approve`; approver must differ from the discoverer | `tests/artifact/test_artifact_lock_approval.py` | one approver, no dual control |
| Commit at most once | single click, request id, startup sweep marks unknown | `test_commit_clicked_once_on_lost_response`, `test_same_request_id_returns_stored_result_with_no_ui_action` | outcome after a lost response needs a person |
| Secrets and personal data | redactor on every evidence write; `cua lint` over `evidence/` | `tests/safety/test_redact.py`, `tests/evidence/test_evidence_lint.py`, `evidence/evidence_lint.txt` | lint reads text files; screenshots are masked, not scanned |
| No model in replay | import rule | `tests/replay/test_replay_imports.py` | discovery sends page text to a model |
| Human control | lease generation | `tests/control/test_lease.py` | console has no authentication |

- Operator console: the Origin check on POSTs is skipped when the request has no Origin header (non-browser clients). Exposure is limited by loopback-only binding, the Host allow-list and JSON-only bodies; there is no authentication.

## 7. Cuts

- Second tenant (renamed labels, different module set): not built; milestone waived (`docs/implementation-status.md`).
- Second model provider: not built; one adapter (`tests/agent/test_anthropic_adapter.py`).
- Payment discovery finished from a recorded transcript when API credits ran out (`evidence/discovery_record_payment/result.json`: `model: scripted`); its `model` tests were not run.
- Differential read-back, in-doubt hold, threshold dual control, automated cleanup of test payments.
- Frame handling, popups, generated per-version ids (§4).
- Live tests need Docker and skip without it; the committed evidence is then the record.
