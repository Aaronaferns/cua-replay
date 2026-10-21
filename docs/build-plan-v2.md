# Build Plan v2 (Option B: Balanced, target = Dolibarr)

> **Status (2026-10-21):** the plan as it stood before the build, kept as written. What was actually built, measured
> and cut is in `docs/implementation-status.md`; the system as built is described in `REPORT.md`.

This is the plan Claude Code executes. The reasoning behind it is in `architecture-review-v2.md`. The target-app
evidence is in `spikes/dolibarr/README.md`. `DESIGN.md` is superseded wherever these documents disagree.

**Budget:** about 9.5 ± 1 focused days.

**Gates:**

| Gate | Condition | If it slips |
|---|---|---|
| G1 | M2 is green by end of day 3–3.5 | Apply the cut list |
| G2 | M4 is green by end of day 6 | Apply the cut list |
| G3 | M7 is submit-ready | Freeze features |

**Cut order when a gate slips** (cut item 1 first):
1. settle jitter ablation
2. differential read-back
3. tenant B
4. coverage printout and `--repeat`
5. approval-token binding
6. policy preflight bundle
7. `request_id` ledger (at this point Option B has become Option A)
8. injected interstitial (keep the unknown dialog)

**Never cut:**
- the real discovery run;
- the read-flow taxonomy;
- the write flow through commit with commit-unknown;
- the CAPTCHA handoff with the human-commit guard;
- evidence lint.

**Rules that apply to every milestone**

- **Design-only items are never imported, stubbed or added to schemas or enums:**
  - effect `write`;
  - family and version binding layers;
  - `RunHandle`;
  - desktop and pixel surfaces;
  - coordinate actions;
  - read-back fields (until read-back is actually built);
  - policy hierarchy;
  - threshold expressions;
  - frames handling beyond what Playwright gives for free.
- **Fault harness:** `src/cua/faults/` is installed only when the policy marks the target as sandbox. `replay` never
  imports it.
- **Target REST API:** used only by fixtures (`target/dolibarr/seed.py`), tests and evidence ground truth. Replay and
  discovery never call it; they use the UI.
- **Concept budget:** a named concept appears in REPORT.md only if a test name or an evidence path shows it.
- **Stop rule:** if a subsystem runs past 1.5× its time box, take its documented fallback.
- **When writing model-adapter code,** load the `claude-api` skill first and verify current API rules.
- **Windows dev loop:**
  - keep WSL alive while working (a background `wsl -- sleep` or `vmIdleTimeout=-1`); containers use
    `restart: unless-stopped`;
  - Python clients use `127.0.0.1`, not `localhost`;
  - set `PYTHONIOENCODING=utf-8`.

```
M0 ─► M1 ─► M2 ─► M3 ─► M4 ─► M5 ─► M6 ─► M7 ─► (M8 optional)
 S9,S10   target    slice    schema    faults    handoff   commit    harden+submit
 S2,S6,S7 Surface   (G1)     freeze    (G2)      CAPTCHA   payment   (G3)
                    S3       S4,S5
```

---

## M0: Repo hygiene + remaining spikes (≈0.5 d)

**GOAL:** a publishable repo, plus answers to the Dolibarr questions the first spike didn't cover.

**FILES / MODULES:** `pyproject.toml`, `.gitignore`, `.gitattributes`, `.env.example`, `spikes/dolibarr/*` (exists),
`spikes/README.md`, `REPORT.md` (skeleton).

**IMPLEMENT**
- `git init` inside `cua`. It currently sits in a repo rooted at `Projects`; move it to a path without spaces if
  possible.
- `.gitignore`: `.env`, `desktop.ini`, `spikes/**/out/`, `runs/`.
- `.gitattributes`: `* text=auto eol=lf`.
- `uv init`; pin `playwright==1.63.*`. Chromium 1243 is already installed.
- **S9** (1 h) payment commit through the UI:
  - record a payment on a validated invoice;
  - an overpayment shows Dolibarr's own error and creates no payment;
  - the lost-504 fault on the payment confirm leaves exactly one payment in `GET /invoices/{id}/payments`.
- **S10** (30 min) `mariadb-dump` after seeding, then restore. The restore must take < 15 s and give identical REST
  state.
- **S2** (2 h) one `asyncio.run()` hosting `uvicorn.Server.serve()` + headed Playwright on Dolibarr:
  - pause/resume over an `asyncio.Event`;
  - capture of click, change, submit and Enter across full-page posts;
  - `context.on("request")` logs human `action=confirm_*` GETs;
  - clean Ctrl+C.
- **S6** (30 min) do user-opened tabs, `file://` and DevTools escape a deny-all `context.route`?
- **S7** (30 min) normalized-exact matching over captured Dolibarr labels. It must hit "Current outstanding bill" and
  never "… late", and handle trailing `:`, case and `&nbsp;`.
- **REPORT.md skeleton:** the 7 headings, a word budget (≈250/350/400/250/300/220/200) and the concept-budget rule.

**TEST:** the spike scripts.

**ACCEPTANCE CRITERIA:** `spikes/README.md` records pass/fail for S9, S10, S2, S6 and S7.
- **S9 fail:** the commit capability becomes invoice validation, which is already proven.
- **S2 fail:** console on a thread; capture via request log + form diffs.

**DEMO ARTIFACT:** `spikes/README.md`.

**DO NOT BUILD YET:** any schema, any model call, tenant B.

---

## M1: Target + Surface (0.75–1 d; depends on M0)

**GOAL:** a resettable real target, and a guarded way to observe and act on it.

**FILES / MODULES:**
- `target/dolibarr/{docker-compose.yml, seed.py, reset.sh, captcha.sh}`;
- `src/cua/surface/{base,web,snapshot}.py`;
- `src/cua/safety/{policy,network,redact}.py`;
- `apps/dolibarr/profile.json` (entry + login flow only);
- `policies/dolibarr-sandbox.json`;
- `tests/`.

**IMPLEMENT**
- **Compose:** `dolibarr/dolibarr:23.0.4` + `mariadb:11.4`, demo data, modules `Societe,Facture,Banque,Api`, port 8085,
  `restart: unless-stopped`. Credentials come from `.env`.
- **`seed.py`** (REST, admin key from `.env`) creates known fixtures:
  - customers with known codes (`CU1910-00021` from the demo data makes `CU1910-0002` a near-miss);
  - unpaid validated invoices with known amounts;
  - a read-only user and a no-rights user;
  - a bank account.
- **`reset.sh`** restores the post-seed dump (S10).
- **`Surface.observe()`** returns:
  - the AI snapshot **scoped to the main content area** plus any dialog layer, with menus summarized as link names
    (spike: full pages are 32–39k characters);
  - a snapshot epoch;
  - a screenshot (`device_scale_factor=1`);
  - page facts.
- **`Surface.perform()`** acts by `aria-ref` with a `count()==1` precheck; `type` clears the field, then types.
- **Policy file and guard:** origins (`localhost:8085`), path rules, allowed action types.
- **Network routing:** `context.route` allowlist, scheme check, `service_workers="block"`, `accept_downloads=False`.
- **Evidence writer:** the single persistence path, with known-value + pattern redaction and partial masks; `SecretStr`
  for secrets.
- **Scripted login flow:** the harness logs in. The model never sees credentials.

**TEST:** `test_guard_denies_action_type`, `test_network_blocks_off_allowlist`, `test_redactor_known_values_and_variants`,
`test_observe_scopes_to_main_content`, `test_reset_restores_known_state`.

**ACCEPTANCE CRITERIA:**
- a scripted (non-model) driver opens a seeded customer by refs and reads "Current outstanding bill";
- that value matches the REST view of the customer's unpaid invoices;
- an off-allowlist navigation is blocked and logged.

**DEMO ARTIFACT:** none committed.

**DO NOT BUILD YET:** fault harness, write flow, tenant B, artifact schema.

---

## M2: Thin vertical slice (1–1.5 d; depends on M1) **G1: green by end of day 3–3.5**

**GOAL:** goal → real model run → recorded trace → artifact → model-free replay.

**FILES / MODULES:**
- `src/cua/agent/{loop,actions,model,anthropic_adapter,scripted_adapter,stuck}.py`;
- `src/cua/compiler/{recorder,locators,compile}.py`;
- `src/cua/artifact/schema.py` (v0);
- `src/cua/replay/{resolver,interpreter}.py` (v0);
- `src/cua/cli.py` (`discover`, `replay`).

**IMPLEMENT**
- **S3 first** (1 h): the model on `lookup_customer_outstanding`. Compare scoped text only vs text + screenshot, and
  record tokens per turn.
- **Closed action schema** as strict tools, each with `rationale`: `click(ref)`, `type(ref,text)`, `select`, `press`,
  `navigate`, `extract(name,ref)`, `request_approval`, `request_help`, `done(evidence_refs)`.
- **Loop:** one action per turn, all tools declared on the first request, append-only history.
- **Tokens:** `{{input}}` substitution in the executor. A raw input value in *any* string argument is rejected; control
  characters are rejected.
- **Adapters:** `ModelAdapter.next_action(obs)`. Implementations: Anthropic SDK (`claude-opus-5-5`, effort set
  explicitly; OpenRouter fallback by config) and a scripted adapter that can load a recorded transcript.
- **Stuck detector:** the screen signature includes field values; plus a 30-step budget and a wall-clock limit.
- **Recorder:** redacted trace, plus per acted-on ref: candidates, fingerprint and token occurrences.
- **Locator candidates on Playwright's engine:**
  1. role + normalized-exact name;
  2. `same_row` (label cell → control or value cell);
  3. `table_cell`;
  4. scoped (by column header for list filters: Dolibarr's six filter textboxes share one name);
  5. `normalize()`, ranked low;
  6. CSS last.

  Keep a candidate only if it matches 1 of 1 and is the same element as the ref.
- **Compiler v0:** validate the run → keep actions → lift tokens → strategies → attach states from a 4-state profile.
- **Replay v0:** exactly-one resolution, act, wait for the expected state, extract, success.

**TEST:** `test_replay_never_imports_model_code`, `test_tokens_reject_raw_values_all_args`, `test_loop_stops_on_budget`,
`test_scripted_adapter_loop`, `test_candidate_same_element_as_ref`.

**ACCEPTANCE CRITERIA:**
- the real discovery run is committed;
- the compiled artifact is committed;
- `cua replay dolibarr.lookup_customer_outstanding --input customer_code=<a different seeded customer>` returns
  `success`, its amount equals the REST ground truth, and it makes zero model calls.

**DEMO ARTIFACT:** `evidence/discovery_lookup_customer_outstanding/` (manifest, events.jsonl with rationales, redacted
transcript, trace, masked screenshots, draft).

**DO NOT BUILD YET:** faults, handlers, lease or console, write flow, approval, set-of-marks, observation label hints, a
second adapter.

---

## M3: Artifact v1 + deterministic replay core (1.25–1.5 d; depends on M2) **schema freeze at exit**

**GOAL:** the schema reviewers will grade, and a classifier that can be trusted.

**FILES / MODULES:**
- `src/cua/artifact/{schema,predicates,lock,approval}.py`;
- `src/cua/replay/{classifier,resolver,interpreter}.py`;
- `src/cua/compiler/{compile,review_card}.py`;
- `apps/dolibarr/profile.json` (screen catalog);
- `capabilities/dolibarr/lookup_customer_outstanding@1.0.0.json`, `.draft.json`, `.tests.json`, `.review.md`;
- `approvals/`.

**IMPLEMENT**
- **Schema** (pydantic, `extra="forbid"`):
  - `Strategy{kind, …, why, matched_at_record}`;
  - `Fingerprint{role, tag, input_type, frame, row_label, column_header}`;
  - `Target`;
  - `Step{id, intent, expect, do, effect: none|read|commit, navigates, on, verify, timeout_ms}`;
  - `Contract{inputs, outputs, outcomes}`;
  - `success{state, outputs, checks}`.

  No status inside the hashed body.
- **Predicates:** `url_matches`, `text` (normalized-exact or anchored pattern; role and frame optional),
  `target_visible`, `target_equals_input` (normalizer), `all`, `any`, `not`.
- **Profile screen catalog** (run **S4** first):
  - states: login, login-with-CAPTCHA, home, customer list, `customer_not_found` (`specializes` the customer list),
    customer card, invoice card, payment form, confirmation dialog, access denied;
  - `origin` and `edited` per state;
  - `default_meaning`;
  - handlers (`then: reclassify|restart_if_retry_safe`, `max_per_run`, effect none only);
  - `entry`;
  - commit routes by URL pattern (`action=confirm_*`, payment add), because Dolibarr commits are GET requests with a
    CSRF token.
- **Compiler:**
  - template-proposed observed states (URL glob + first main-content heading) + a collision check;
  - derived value echo;
  - identity assert (the "Customer Code" value cell equals the input);
  - static validation, including outputs produced on every success path and no literals or PII in finders.
  - Run **S5** after the first compile.
- **Classifier:**
  - context-free and pure;
  - settled = main content and dialog layer stable on 2 polls ≥150 ms apart, with the navigation fence passed;
  - content-based, because the login form appears at the same URL;
  - dialog layer first;
  - overlap legal only via `specializes`;
  - `Unknown` carries near-misses.
- **Resolver:**
  - normalized-exact names (**S7**);
  - the fingerprint gates any non-primary hit;
  - primary-only resolution for output extracts;
  - `drift[]` when a fallback is used.
- **Lock and approval:**
  - `lock_hash = sha256(canonical(capability) ‖ canonical(profile) ‖ canonical(overlay))` over parsed JSON;
  - approval sidecar `{lock_hash, approver, at, validation_run_ids}`;
  - reject approver == discoverer.
- **Suite and tooling:** `tests.json` runner (`cua validate`, resets the target per case); review card;
  `cua capabilities --json`; deterministic `cua compile <trace>`.

**TEST:** `test_classifier_on_stored_snapshots`, `test_specializes_resolves_not_found`,
`test_login_form_same_url_classified_by_content`, `test_ambiguous_state_is_failure`,
`test_resolver_rejects_outstanding_late`, `test_fingerprint_blocks_misbound_fallback` (shared-name filter inputs),
`test_extract_primary_only`, `test_lock_hash_crlf_stable`, `test_profile_edit_invalidates_approval`,
`test_self_approval_rejected`, `test_compile_from_trace_reproduces_draft`, `test_artifact_has_no_playwright_concepts`,
`test_finders_contain_no_input_or_pii`.

**ACCEPTANCE CRITERIA:**
- the suite passes for two customers;
- the draft hash is reproducible from the committed trace;
- **the schema is frozen.**

**DEMO ARTIFACT:** `evidence/replay_success_other_customer/`, `capabilities/.../*.review.md`.

**DO NOT BUILD YET:** tenant overlay, coverage-gated approval, waivers, `capture-state`, all-finder consensus,
outcome payloads.

---

## M4: Runtime failures + outcomes (1–1.25 d; depends on M3) **G2: green by end of day 6**

**GOAL:** every runtime condition in brief §3.3, reproduced on Dolibarr and reported deliberately.

**FILES / MODULES:** `src/cua/faults/harness.py`, `src/cua/replay/{handlers,result,interpreter}.py`,
`src/cua/safety/redact.py` (DOM masks), `target/dolibarr/session.sh` (server-side session deletion),
`capabilities/.../lookup_customer_outstanding.tests.json`.

**IMPLEMENT**
- **Fault harness** (Playwright routes; sandbox-only):

  | Fault | What it does |
  |---|---|
  | `slow:<ms>` | Delays matching requests |
  | `app_error` | Fulfills a matching request with a 500 page |
  | `interstitial` | Injects a known modal into the real HTML |
  | `unknown_dialog` | Injects a modal no profile knows |

- **Session expiry:** delete the PHP session server-side, so the next navigation shows the login form; the re-login
  handler then restarts if retry-safe.
- **Data-driven outcomes:**
  - an unknown code → `customer_not_found`;
  - code `CU1910-0002` → the only row is `CU1910-00021` → `failure(IDENTITY_MISMATCH)`, no amount returned;
  - the no-rights user → `access_denied`.
- **Meaning order:** step `on` → handler (content-positive trigger) → profile `default_meaning` → unknown.
- **`RunResult`:**
  - four statuses: `success | business_outcome | failure | indeterminate`;
  - `failure{code, category, step_id, step_intent, expected, observed{state, url, near_misses}, detail, evidence, approvable}`;
  - `recoveries[]` on every status, plus `drift[]`;
  - `retry_safe` computed from the effect high-water mark;
  - masked `observed`.
- **Failure evidence:** a masked screenshot with declared masks + a generic `input, select, textarea, td` mask, and a
  redacted snapshot.
- **Errors:** `hide_input_in_errors=True` on all models; errors re-messaged at the boundary.
- **`make-evidence`:** regenerates the replay bundles from `tests.json`, resetting the target per case.

**TEST:** one per taxonomy row, plus `test_unknown_dialog_fails_closed`, `test_near_miss_is_identity_mismatch`,
`test_slow_within_timeout_not_failure`, `test_slow_beyond_timeout_transient_exhausted`, `test_pydantic_errors_hide_inputs`,
`test_replay_never_imports_faults`.

**ACCEPTANCE CRITERIA:** every suite case lands in its expected status, outcome and recoveries, 3 consecutive runs.

**DEMO ARTIFACT:** `replay_not_found/`, `replay_nearest_match/`, `replay_interstitial_recovered/`,
`replay_unknown_dialog/`.

**DO NOT BUILD YET:** extra fault types, tenant B.

---

## M5: Human escalation + handoff (1.25–1.75 d; depends on M4 and S2) **system is submittable at exit**

**GOAL:** a real control transfer on the same live session, with a known holder at all times.

**FILES / MODULES:** `src/cua/control/{lease,interventions,capture,console,resume}.py`, CLI `cua console`,
`target/dolibarr/captcha.sh`.

**IMPLEMENT**
- **Lease:** `{holder: automation|human:<id>|none, generation, reason, since}`, in memory, with transitions logged.
  - The guard checks holder and generation on every automated perform.
  - Drain to a step boundary before release.
  - Automation never reclaims control.
  - The SLA timeout applies only to unclaimed requests.
- **Handoff trigger:** Dolibarr's login CAPTCHA is on for the demo. After a server-side session expiry, the scripted
  re-login sees `textbox "Security code"`, which raises a `HUMAN_ONLY_LOGIN` intervention.
- **Intervention request** (JSON in the run bundle): capability, step, reason code, classified and expected state, URL,
  masked screenshot, effect so far, lease.
- **Console** (minimal FastAPI page): queue, context, screenshot, and claim / resume / abort / approve / mark-complete /
  verdict.
- **Capture:** binding + init script (click, change, submit, Enter, target descriptor, values redacted in the page —
  the CAPTCHA value too) and a request log during human control. Events are tagged `actor: human`.
- **Resume:** restart from the entry on the same session, at most once per handoff.
  - Forbidden if a commit was attempted (by automation, a captured human click, or a request-log hit on a commit
    route).
  - Forbidden if the high-water effect is above read.
  - Mark-complete passes only through the success checkpoint + output extraction.
- **CLI:** blocks while escalated and emits an interim `escalated` event line.

**TEST:** `test_stale_generation_cannot_act`, `test_automation_never_reclaims_from_human`,
`test_pause_before_request_visible`, `test_sla_only_for_unclaimed`, `test_mark_complete_requires_checkpoint`,
`test_captcha_login_escalates`, and the resume matrix.

**ACCEPTANCE CRITERIA:** the CAPTCHA bundle shows lease generations, captured human actions with redacted values,
re-entry, and a final `success` whose amount matches REST.

**DEMO ARTIFACT:** `replay_captcha_handoff/`.

**DO NOT BUILD YET:** CDP screencast, remote console, RunHandle or daemon, rewind-to-earliest-match, `CONTROL_CONFLICT`
detection.

---

## M6: Write flow + commit safety (1.25–1.5 d; depends on M5 and S9)

**GOAL:** the money-moving path, at most once and never over-claimed, checked against Dolibarr's own data.

**FILES / MODULES:** `src/cua/replay/commit_ledger.py`, interpreter/guard changes, fault `lost_response_after_commit`,
`capabilities/dolibarr/record_invoice_payment@1.0.0.*` (or `validate_invoice` if S9 failed).

**IMPLEMENT**
- **Capability:** `record_invoice_payment`. Inputs: `invoice_ref`, `amount`, `method`, `bank_account`, `request_id`.
  Output: `payment_ref`. Outcomes: `invoice_not_found`, `validation_rejected` (overpayment), `access_denied`.
- **Effect classification:**
  - commit routes come from the profile;
  - the regex can only raise a class;
  - an unnamed submit in a dirty form counts as commit;
  - the capability's declared effect is a ceiling in both discovery and replay.
- **Discovery:** a commit needs a console approval bound to (action hash, generation), single use. Run real discovery.
- **Commit path:**
  1. Pre-commit verification: every review field equals its input (amount, invoice, bank account).
  2. Record the attempt marker before the click (and on a captured human commit or a request-log hit).
  3. After the attempt: confirmation (payment shown, fields equal inputs) ⇒ `success`; declared rejection ⇒
     `business_outcome`; anything else ⇒ `indeterminate(COMMIT_UNKNOWN)`, `retry_safe=false`, escalate.
  4. No re-entry into a commit step.
  5. Operator verdict `resolve_commit`.
- **Fault `lost_response_after_commit`:** `route.fetch()` sends the real confirm request, then `route.fulfill(504)`.
- **`request_id` ledger** (B):
  - a durable row with a conditional PREPARED→ATTEMPTED update;
  - a stored result is returned with zero UI actions;
  - reuse with different inputs is rejected;
  - startup sweep: ATTEMPTED → UNKNOWN.

**TEST** (the oracle is `GET /invoices/{id}/payments`, count and amounts): `test_commit_clicked_once_on_lost_response`,
`test_relogin_never_reclicks_commit`, `test_unconfirmed_commit_is_indeterminate`, `test_overpayment_is_business_outcome`,
`test_review_mismatch_blocks_commit`, `test_draft_cannot_commit`, `test_effect_ceiling`,
`test_resume_after_human_commit_never_commits` (scripted human), `test_same_request_id_returns_stored_result`,
`test_startup_sweep_marks_unknown`.

**ACCEPTANCE CRITERIA:**
- exactly one new payment in every commit test;
- the commit bundle shows the attempt event sequenced before the single click, the browser's 504, `indeterminate`,
  REST ground truth (one payment), and a recorded verdict.

**DEMO ARTIFACT:** `discovery_record_payment/`, `replay_commit_unknown/` (with `ground_truth.json` from REST before
and after).

**DO NOT BUILD YET:** differential read-back, in-doubt hold, threshold dual control, automated cleanup.

---

## M7: Safety + evidence hardening and submission package (1–1.25 d; depends on M6) **G3: SUBMIT-READY**

**GOAL:** every claim checkable; the repo runs from a clean clone.

**FILES / MODULES:** `src/cua/safety/lint.py`, `src/cua/cli.py` (`demo up`, `validate --repeat`, `make-evidence`),
`evidence/README.md`, `README.md`, `REPORT.md`.

**IMPLEMENT**
- **Evidence lint:** scans `evidence/**` for `.env` secret values, SSN patterns, cookie headers and unmasked demo-data
  e-mail addresses. Output goes to `evidence/evidence_lint.txt`.
- **Static policy preflight** (B): a policy denying the payment route → `replay_policy_denied` bundle, with the UI
  untouched.
- **`cua validate --repeat N`:** writes `evidence/validation_suite.json` (n, cases, flakes).
- **`cua demo up`:** `docker compose up -d`, wait for health, reset, print the URL. Plus a note on keeping WSL alive on
  Windows.
- **README:**
  - prerequisites: Docker, uv, an API key for discovery only;
  - setup;
  - what runs **without** Docker (committed evidence, offline unit tests) and without a key (replay);
  - exact demo commands in PowerShell and bash.
- **`evidence/README.md`:** bundle → requirement → where to look, including `ground_truth.json`.
- **REPORT.md** under the word budget and concept budget:
  - a taxonomy table with evidence paths;
  - a Safety table in the form "control → enforced by → shown in → limit";
  - §1 states why Dolibarr: "a real app; we inject only network-level faults and declare them";
  - §4 names the missing frameset;
  - §7 Cuts.

  Read it cold and fix any guessed term.
- **Clean-clone run** on WSL2 Ubuntu and on Windows.

**TEST:** `test_evidence_lint`, README smoke script.

**ACCEPTANCE CRITERIA:**
- lint is clean;
- the README commands work from a clean clone on both OSes;
- REPORT ≤ 3 pages and every claim names a test or bundle;
- no MPC, HER, "closed-loop policy", background-differentiator text, vendor version strings or product lists anywhere
  shipped.

**DEMO ARTIFACT:** the complete `evidence/`, `README.md`, `REPORT.md`.

**DO NOT BUILD YET:** anything in M8 until this milestone is green.

---

## M8: Optional differentiators (only if ahead; in order; each boxed)

| # | Item | Box | Done when |
|---|---|---|---|
| 1 | **Tenant B:** a second Dolibarr container, same version. Dolibarr's translation overrides rename labels (for example "Customer Code" → "Member Number"); a different module set; CAPTCHA on. One overlay file (strategies, signature text, enabled handlers only). | 0.5 d | Two bundles: without the overlay → fail-closed `TARGET_NOT_FOUND` with near-misses and `drift`; with it → `success` matching REST |
| 2 | **Differential read-back** on `COMMIT_UNKNOWN`: before/after payments list on the invoice card; confirms, never refutes | 4 h | The lost-response variant reconciles to `success` (`recoveries: commit_reconciled`); a no-posting variant stays `indeterminate` |
| 3 | **Settle-rule jitter ablation:** 20 runs with randomized `slow:` timing, rule on vs off | 2–3 h | "k/20 misclassified without the rule, 0/20 with it" |

**Rule:** each item ends with `make-evidence`, a lint pass and a REPORT edit, or it is reverted.

**DO NOT BUILD (design text only):**
- WebArena Verified;
- model comparison, UI-TARS/vLLM, Anthropic native toolset adapter;
- OCR masking;
- the family → version → tenant chain;
- RunHandle daemon;
- CDP screencast console;
- model-assisted recovery;
- desktop, IE and VDI adapters;
- framesets;
- quarantine and canaries;
- scoped re-discovery;
- coverage-gated approval and waivers.
