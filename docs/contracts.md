# Shared contracts and ownership

The working agreement three implementation agents (A, B, C) and a lead used to build this in parallel without
silently changing each other's interfaces; `docs/build-plan-v2.md` was the plan. Ownership, the live-target ports and
the git rules are a record of how the build ran. The shared-type table and the import rules still describe the code.

## Ownership

| Area | Owner | Paths |
|---|---|---|
| Target + runtime + discovery | A | `target/dolibarr/`, `src/cua/surface/`, `src/cua/agent/`, `src/cua/compiler/recorder.py`, `src/cua/compiler/locators.py`, `src/cua/compiler/trace.py` (B reviews every change) |
| Artifact + replay + correctness | B | `src/cua/artifact/`, `src/cua/compiler/compile.py`, `src/cua/compiler/review_card.py`, `src/cua/replay/`, `capabilities/`, `apps/dolibarr/profile.json`, `approvals/` |
| Safety + control + integration | C | `src/cua/safety/`, `src/cua/control/`, `src/cua/evidence/`, `src/cua/faults/`, `src/cua/config.py`, `policies/`, `tests/integration/`, `evidence/`, `README.md`, `REPORT.md` |
| Integration + status | Lead | `docs/implementation-status.md`, `docs/contracts.md`, `src/cua/cli.py`, `tests/conftest.py`, `pyproject.toml`, `uv.lock` |

Tests live next to their owner: `tests/<area>/test_*.py` (`tests/surface`, `tests/agent`, `tests/artifact`, `tests/replay`,
`tests/safety`, …). Commands: each package's `commands.py` exposes `register(subparsers)` (see `src/cua/cli.py`).
Adding a dependency to `pyproject.toml` is a request to the lead, not an edit.

## Shared types (seeded on `main`, owner evolves them)

| Type | Module | Owner | Main consumers |
|---|---|---|---|
| `Effect`, `FieldSpec`, `Strategy`, `Fingerprint` | `artifact/schema.py` | B | A (recorder, locators), C (policy) |
| `Observation`, `PageFacts`, `Element`, `Surface` | `surface/base.py` | A | B (classifier, resolver), C (capture, masks) |
| `AgentAction` | `agent/actions.py` | A | B (compiler via trace) |
| `RecordedTrace` and parts | `compiler/trace.py` | A, B reviews | B (compiler) |
| `ActionRequest`, `PolicyDecision`, `PolicyDenied`, `Guard` | `safety/policy.py` | C | A (surface), B (interpreter) |
| `ControlLease`, `LeaseToken` | `control/lease.py` | C | A, B |
| `EvidenceEvent`, `EvidenceWriter` (protocol); `RunBundleWriter` | `evidence/events.py`, `evidence/writer.py` | C | A, B |
| `RunResult`, `Failure`, `Recovery`, `DriftWarning`, `SideEffects` | `replay/result.py` | B | C (console, evidence), CLI |
| `Settings` | `config.py` | C | everyone |
| `Session`, `open_session` | `control/runtime.py` | C | A (discover), B (replay/validate CLI) |
| `LeaseManager`, `LeaseEvent` | `control/lease.py` | C | B (interpreter `step_boundary`), C (console) |
| `FaultSpec`, `install_faults` | `faults/harness.py` | C | tests, CLI `make-evidence` (never replay) |
| `RoutePattern{method, body}`, `is_commit` | `artifact/schema.py`, `artifact/routes.py` | B | C (commit detection, capture) |
| `Policy`, `PolicyGuard`, `new_guarded_context`, `BlockedRequest` | `safety/policy.py`, `safety/network.py` | C | A (surface tests), CLI wiring |
| name normalizer (S7) | `artifact/names.py` | B | A (surface), B (resolver, classifier) |
| `Profile`, `LoginForm`, `Target` | `artifact/schema.py` | B | A (scripted login), C (commit routes) |

Changing a shared type someone else consumes: describe the change and why to the lead, keep the old shape working
(or mock the boundary) until the lead integrates the owner's commit, then consumers move. Never redefine another
owner's type locally.

## Import rules

```
artifact   leaf: pydantic only, no Playwright vocabulary
surface    → artifact, safety.policy, control.lease, config
safety     → artifact, control.lease, config
control    → artifact, surface, safety, evidence, config  (never replay; replay registers callbacks)
evidence   → safety, control.lease, config
agent      → surface, safety, control, evidence, compiler, artifact
compiler   → artifact, agent.actions, surface, safety.policy
replay     → artifact, surface, safety, control, evidence      NEVER agent, compiler.recorder, faults, anthropic,
                                                                 target REST helpers
faults     → playwright, pydantic, safety.policy only; installed by tests/CLI only when the policy marks the target sandbox
cli        → anything; each package's `commands.py` is CLI wiring and may import lazily (inside command functions)
             beyond its package's rule, e.g. `validate`/`make-evidence` reach target REST helpers and faults
```

Tests check these rules unevenly. `artifact`, `safety`, `evidence`, `replay` and `faults` are held to their full
allow-lists (`tests/artifact/test_artifact_schema.py::test_artifact_imports_only_pydantic_and_the_standard_library`,
`tests/safety/test_import_rules.py`, `tests/replay/test_replay_imports.py`,
`tests/faults/test_harness.py::test_faults_import_only_playwright_pydantic_and_the_policy_type`). For `compiler` and
`agent` only the forbidden imports are checked: the compiler never loads replay, faults, the recorder, Playwright or
the model SDK (`tests/compiler/test_compile_v0.py`), and only `agent/anthropic_adapter.py` loads the SDK
(`tests/agent/test_agent_imports.py`). No test checks `surface` or `control`.

`src/cua/agent/__init__.py` stays empty so importing `cua.agent.actions` never loads the model SDK.

Seams: production browser contexts come only from `cua.safety.network.new_guarded_context` (the surface takes a Page
and a Guard by injection). There is one name normalizer, `cua.artifact.names`; `surface` delegates to it.

## Live target

There is one Dolibarr target per agent while agents work in parallel, so resets don't collide:
`DOLIBARR_PORT` 8085 (A), 8086 (B), 8087 (C), each its own compose project (`cua-8085`, `cua-8086`, `cua-8087`). Live
tests skip when the target is not reachable. Tests marked `live` must reset the state they depend on.

## Git

- Branches: A `impl/runtime-discovery`, B `impl/artifact-replay`, C `impl/control-integration`; lead integrates on `main`.
- Commit coherent tested units: `<type>(<area>): <description>`, types feat/fix/test/refactor/docs/chore.
- Before every commit: `git status`, `git diff`, `git diff --staged`; stage explicit paths, never `git add .`.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Never commit `.env`, keys, cookies, browser state, DB dumps, HAR/traces, `runs/`, `tmp/`, unredacted screenshots.
- Handoffs happen through commit SHAs reported to the lead. Nothing important lives only in a working tree.
- No force push, `reset --hard`, history rewrites, or rebasing commits someone depends on. Unfamiliar changes are
  preserved until their owner is known.

## Scope firewall

Never implement, stub, enum or schema-field any design-only item: effect `write`; family/version binding layers;
RunHandle/daemon; desktop, pixel or coordinate actions; policy hierarchy or threshold expressions; model-assisted
replay recovery; WebArena; model comparison; UI-TARS/vLLM; Anthropic browser-toolset adapter; OCR masking;
Selenium/IE; CDP screencast console; distributed workers; quarantine/canaries; scoped re-discovery;
coverage-gated approval; waivers; generalized auto-repair; frameset support beyond Playwright's defaults.
Tenant B and differential read-back were cut (REPORT §7); the settle-jitter experiment was never run.

Evidence event kinds in use: `observe`, `action`, `policy`, `state`, `recovery`, `drift`, `commit_attempted`
(automation, written before the click; a human's commit click during a handoff is `commit_attempt`), `escalated`,
`handoff_ended`, `lease`, `intervention`, `result`, `fault` (declared, sandbox only).
