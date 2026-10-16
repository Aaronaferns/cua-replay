# Implementation status

Source of truth for milestone state. A milestone is GREEN only when its acceptance criteria pass, its tests pass on the
integrated branch, required evidence exists, the work is committed, and the working tree is understood.
Plan: `docs/build-plan-v2.md` (supersedes DESIGN.md, which is a local working document and not shipped).

Statuses: NOT STARTED · WORKING · BLOCKED · GREEN · CUT

| Milestone | Owner | Status | Acceptance Criteria | Tests | Evidence | Integrated Commit | Blocker | Decision |
|---|---|---|---|---|---|---|---|---|
| M0 Repo + spikes | Lead (+A, B, C) | GREEN | `spikes/README.md` records S9, S10, S2, S6, S7 | spike scripts | `spikes/README.md` | — | — (2026-10-12: disk freed, WSL rw, API key set) | S9, S10, S2, S6, S7 all PASS and recorded in `spikes/README.md` (S3 recorded under M2). Repo kept at current path (moving would break the open workspace); scripts quote paths. |
| M1 Target + Surface | A (+C, B profile) | GREEN | scripted UI read == REST; off-allowlist navigation blocked + logged | `test_guard_*`, `test_network_*`, `test_redactor_*`, `test_observe_*`, `test_reset_*`, `tests/integration/test_m1_acceptance.py` | — | `ce20237` | — | Parts integrated 2026-10-12: target + seed + S10 reset + surface + scripted login (A), guard + network + redactor + evidence writer (C, validator PASS), profile (B). Live scripted read == REST passes with a test guard. M1 acceptance (a)(b)(c) passes live through `open_session` (real guard + network; off-allowlist navigate and in-page request both blocked and logged). Full suite on `ce20237`: 658 passed, 0 skipped, live included (2026-10-12). |
| M2 Thin vertical slice (G1) | A + B (+C) | GREEN | real discovery → real trace → compiled artifact → different-customer replay == REST, 0 model calls | `test_replay_never_imports_model_code`, `test_tokens_reject_raw_values_all_args`, `test_loop_stops_on_budget`, `test_scripted_adapter_loop`, `test_candidate_same_element_as_ref`, `test_live_g1` | `discovery_lookup_customer_outstanding/` | `28cbe0b` | — | G1 met 2026-10-12. Lead check on main/8085: `cua replay dolibarr.lookup_customer_outstanding --input customer_code=CU2601-00902` → success, 355.20 == REST, no drift, 0 model calls (fresh-process test asserts `anthropic` never loaded). Discovery: claude-opus-5-5, effort high, text-only, 5 turns. |
| M3 Artifact + replay core | B (+A, C) | GREEN | suite passes for 2 customers; draft hash reproducible from trace; schema frozen | all 13 plan M3 tests incl. `test_compile_from_trace_reproduces_draft`, `test_resolver_rejects_outstanding_late`, `test_schema_freeze` | `replay_success_other_customer/`, `capabilities/dolibarr/lookup_customer_outstanding@1.0.0.review.md` | `c264644` | — | `cua validate` 2/2 on main (run `val-20261012-123807`). S5: 0 template edits (target ≤1); templates are URL-only because Dolibarr's main content has no heading role. Lock `77a16d16…`. Approval pending: needs a human approver ≠ discoverer (claude-opus-5-5). |
| M4 Runtime taxonomy (G2) | B (+C) | GREEN | every suite case lands in expected status, 3 consecutive runs | one per taxonomy row (`tests/replay/test_replay_m4.py`), `test_unknown_dialog_fails_closed`, `test_near_miss_is_identity_mismatch`, `test_slow_*`, `test_replay_never_imports_faults`, `test_pydantic_errors_hide_inputs` | `runs/replay_{not_found,nearest_match,interstitial_recovered,unknown_dialog}/` (promotion to `evidence/` at M7) | `342d8e8` | — | G2 met 2026-10-13. `cua validate --repeat 3` 33/33 (11 cases × 3, run `val-20261013-011707`); full suite 782 passed incl. live. `access_denied` is a business outcome. Lock hash changed (`33f26bf4…`): approval must be redone. |
| M5 Human handoff | C (+A, B) | GREEN | real CAPTCHA same-session handoff → success == REST | lease/resume tests | `replay_captcha_handoff/` | — | — | — |
| M6 Write + commit | B (+C) | GREEN | exactly one payment in every commit test; commit-unknown bundle | posting-oracle tests (10 named, tests/replay/test_commit_path.py); `validate` record_invoice_payment 5/5 (val-m6-gate) | `discovery_record_payment/`, `replay_commit_unknown/` | — | API credits exhausted: discovery finished by free transcript replay; `model` tests not run at the gate | Gate 2026-10-15: 840 passed live (-m "not model"); lookup re-validated 33/33 (val-m6-lookup, lock 6b9488eec246); draft lock bc3d3a7c49c4 awaiting approval |
| M7 Harden + submit (G3) | C (all) | GREEN (WSL clean clone pending) | lint clean; clean clone works; REPORT ≤ 3 pages | `tests/evidence/test_evidence_lint.py` (incl. README smoke), `tests/evidence/test_demo.py`, `tests/replay/test_validate.py`, `tests/replay/test_commit_path.py` (policy preflight) | `evidence/` (6 bundles, `validation_suite.json` 33/33 no flakes, `evidence_lint.txt` clean, `replay_policy_denied/`) | see git log M7 | WSL clean clone not run: uv not installed in WSL | Full suite 850 passed live (`-m "not model"`); Windows clean clone 827 passed offline; REPORT 1140 words. M4 `runs/` bundles not promoted (taxonomy rows cite tests). |

## Decisions log

| Date | Decision | Why |
|---|---|---|
| 2026-10-11 | Integration branch is `main` (renamed from the unborn `master`) | No history existed yet |
| 2026-10-11 | `docs/DESIGN.md` and the assignment PDF stay local and untracked (git-ignored) | Owner decision: DESIGN.md is superseded and holds personal notes; the PDF is interface.ai's document |
| 2026-10-11 | Repo stays at its current path (contains spaces) | Moving would break the open editor workspace; all scripts quote paths |
| 2026-10-12 | M8 waived; the run stops at G3 | Owner decision |
| 2026-10-12 | Compose project per target is `cua-<port>` (8085 A, 8086 B, 8087 C) | Port already identifies the agent's target; one less variable |
| 2026-10-12 | `apps/dolibarr/profile.json` stays B's; A's scripted login takes role+name targets and is wired to `Profile.login` at integration | Ownership map; no cross-owner edits |
| 2026-10-12 | No profile `family`/`versions` fields; plain `app` | Family/version binding layers are design-only |
| 2026-10-12 | Seed uses SQL through the db container for API keys and user rights | Dolibarr REST cannot store `api_key` or grant rights; fixtures only, never replay/discovery |
| 2026-10-12 | S9 PASS: the payment is the M6 commit capability (no fallback to invoice validation) | Spike S9 |
| 2026-10-12 | `RoutePattern` gains `method` and `body` (glob) matching; B owns the change, C's commit detection consumes it | The payment commit is a POST with `action=confirm_paiement` in the body; path + query cannot identify it |
| 2026-10-12 | An overpayment warning is `validation_rejected`; automation never answers *Yes* | Dolibarr warns but does not refuse an overpayment (S9 b'') |
| 2026-10-12 | `reset.sh` 15 s budget becomes a warning, not an error | The restore completes in 12–17 s on a loaded host; the budget was a spike target, not a correctness rule |
| 2026-10-12 | "Current outstanding bill" lives on the Customer tab (`/comm/card.php`) | S4 live observation |
| 2026-10-12 | Discovery observes scoped text only; screenshots stay an option (`--screenshots`) | S3: 4 runs, all succeeded; a screenshot adds ≈1.6k tokens/turn (~10%) without fewer steps or errors (indicative, small n) |
| 2026-10-12 | Discovery reaches records by clicking, never by typing ids read off the page; `navigate` rejects numeric record ids | First model run navigated to `?socid=63`, which replays only for that customer |
| 2026-10-12 | Lead promotes curated run bundles from `runs/` into `evidence/` after a secrets/PII scan | `runs/` is git-ignored; one reviewed path into shipped evidence |
| 2026-10-12 | OpenRouter fallback not built | M2 forbids a second adapter; recorded as a cut for REPORT §7 |
| 2026-10-12 | Proposed-state templates are URL-only on Dolibarr | No `heading` role in Dolibarr's main content; catalog content predicates discriminate (S5) |
| 2026-10-12 | `cua replay` runs only approved capabilities unless `--allow-draft` | M3 approval gating; unapproved runs warn |
| 2026-10-12 | Capability approver is the owner (git id `aaronaferns`), recorded with `cua approve` only after the owner reviews the review card, lock hash and a passing validation run | Owner decision; approver must not be the discoverer (claude-opus-5-5) |
| 2026-10-13 | `access_denied` is a business outcome, not failure `PERMISSION_DENIED` | Plan M4: no-rights user → `access_denied` |
