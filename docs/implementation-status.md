# Implementation status

Source of truth for milestone state. A milestone is GREEN only when its acceptance criteria pass, its tests pass on the
integrated branch, required evidence exists, the work is committed, and the working tree is understood.
Plan: `docs/build-plan-v2.md` (supersedes DESIGN.md, which is a local working document and not shipped).

Statuses: NOT STARTED · WORKING · BLOCKED · GREEN · CUT

| Milestone | Owner | Status | Acceptance Criteria | Tests | Evidence | Integrated Commit | Blocker | Decision |
|---|---|---|---|---|---|---|---|---|
| M0 Repo + spikes | Lead (+A, B, C) | WORKING | `spikes/README.md` records S9, S10, S2, S6, S7 | spike scripts | `spikes/README.md` | — | — (2026-10-12: disk freed, WSL rw, API key set) | S7 PASS offline. S10 runs with M1 `reset.sh` (A), S6 with M1 network (C); S9 (B) and S2 (C) run in wave 2 on the M1 target. Repo kept at current path (moving would break the open workspace); scripts quote paths. |
| M1 Target + Surface | A (+C, B profile) | WORKING | scripted UI read == REST; off-allowlist navigation blocked + logged | `test_guard_*`, `test_network_*`, `test_redactor_*`, `test_observe_*`, `test_reset_*`, `tests/integration/test_m1_acceptance.py` | — | `42baca6` | — | Parts integrated 2026-10-12: target + seed + S10 reset + surface + scripted login (A), guard + network + redactor + evidence writer (C, validator PASS), profile (B). Live scripted read == REST passes with a test guard. M1 acceptance (a)(b)(c) passes live through `open_session` (real guard + network; off-allowlist navigate and in-page request both blocked and logged). GREEN once the reset-budget fix (A, `d106125`) is integrated and the live suite passes on main. |
| M2 Thin vertical slice (G1) | A + B (+C) | WORKING | real discovery → real trace → compiled artifact → different-customer replay == REST, 0 model calls | `test_replay_never_imports_model_code`, … | `discovery_lookup_customer_outstanding/` (committed `1b67976`) | `2936ec8` (parts) | — | Real discovery DONE: claude-opus-5-5, effort high, text-only, 5 turns, success; draft compiles byte-identically with `cua compile`. Replay CLI live == REST with a test-fixture capability. Remaining for G1: compile the committed trace into `capabilities/dolibarr/lookup_customer_outstanding@1.0.0.json` and replay beta == REST with 0 model calls. |
| M3 Artifact + replay core | B (+A, C) | NOT STARTED | suite passes for 2 customers; draft hash reproducible from trace; schema frozen | classifier/resolver/lock tests | `replay_success_other_customer/` | — | — | — |
| M4 Runtime taxonomy (G2) | B (+C) | NOT STARTED | every suite case lands in expected status, 3 consecutive runs | one per taxonomy row | `replay_not_found/`, `replay_nearest_match/`, … | — | — | — |
| M5 Human handoff | C (+A, B) | NOT STARTED | real CAPTCHA same-session handoff → success == REST | lease/resume tests | `replay_captcha_handoff/` | — | needs a person at the headed browser | — |
| M6 Write + commit | B (+C) | NOT STARTED | exactly one payment in every commit test; commit-unknown bundle | posting-oracle tests | `discovery_record_payment/`, `replay_commit_unknown/` | — | — | — |
| M7 Harden + submit (G3) | C (all) | NOT STARTED | lint clean; clean clone works; REPORT ≤ 3 pages | `test_evidence_lint`, smoke | `evidence/` | — | — | — |

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
