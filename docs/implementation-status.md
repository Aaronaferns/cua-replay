# Implementation status

Source of truth for milestone state. A milestone is GREEN only when its acceptance criteria pass, its tests pass on the
integrated branch, required evidence exists, the work is committed, and the working tree is understood.
Plan: `docs/build-plan-v2.md` (supersedes DESIGN.md, which is a local working document and not shipped).

Statuses: NOT STARTED · WORKING · BLOCKED · GREEN · CUT

| Milestone | Owner | Status | Acceptance Criteria | Tests | Evidence | Integrated Commit | Blocker | Decision |
|---|---|---|---|---|---|---|---|---|
| M0 Repo + spikes | Lead (+A) | WORKING | `spikes/README.md` records S9, S10, S2, S6, S7 | spike scripts | `spikes/README.md` | — | WSL root FS read-only (host disk full); no `ANTHROPIC_API_KEY` yet | S7 PASS offline. Repo kept at current path (moving would break the open workspace); scripts quote paths. |
| M1 Target + Surface | A (+C) | NOT STARTED | scripted UI read == REST; off-allowlist navigation blocked + logged | `test_guard_*`, `test_network_*`, `test_redactor_*`, `test_observe_*`, `test_reset_*` | — | — | Docker | — |
| M2 Thin vertical slice (G1) | A + B (+C) | NOT STARTED | real discovery → real trace → compiled artifact → different-customer replay == REST, 0 model calls | `test_replay_never_imports_model_code`, … | `discovery_lookup_customer_outstanding/` | — | API key | — |
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
