# Shared contracts and ownership

How three implementation agents work on one codebase without silently changing each other's interfaces.
`docs/build-plan-v2.md` is the plan; this file is the working agreement for building it.

## Ownership

| Area | Owner | Paths |
|---|---|---|
| Target + runtime + discovery | **A** | `target/dolibarr/`, `src/cua/surface/`, `src/cua/agent/`, `src/cua/compiler/recorder.py`, `src/cua/compiler/locators.py`, `src/cua/compiler/trace.py` (B reviews every change) |
| Artifact + replay + correctness | **B** | `src/cua/artifact/`, `src/cua/compiler/compile.py`, `src/cua/compiler/review_card.py`, `src/cua/replay/`, `capabilities/`, `apps/dolibarr/profile.json`, `approvals/` |
| Safety + control + integration | **C** | `src/cua/safety/`, `src/cua/control/`, `src/cua/evidence/`, `src/cua/faults/`, `src/cua/config.py`, `policies/`, `tests/integration/`, `evidence/`, `README.md`, `REPORT.md` |
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
| `EvidenceEvent`, `EvidenceWriter` | `evidence/events.py` | C | A, B |
| `RunResult`, `Failure`, `Recovery`, `DriftWarning`, `SideEffects` | `replay/result.py` | B | C (console, evidence), CLI |
| `Settings` | `config.py` | C | everyone |

Changing a shared type someone else consumes: describe the change and why to the lead, keep the old shape working
(or mock the boundary) until the lead integrates the owner's commit, then consumers move. Never redefine another
owner's type locally.

## Import rules (enforced by tests)

```
artifact   leaf: pydantic only, no Playwright vocabulary
surface    → artifact, safety.policy, control.lease
safety     → artifact, control.lease
control    → artifact, surface, safety, evidence        (never replay; replay registers callbacks)
evidence   → safety, control.lease
agent      → surface, safety, control, evidence, compiler, artifact
compiler   → artifact, agent.actions, surface, safety.policy
replay     → artifact, surface, safety, control, evidence      NEVER agent, compiler.recorder, faults, anthropic,
                                                                 target REST helpers
faults     → playwright only; installed by tests/CLI only when the policy marks the target sandbox
cli        → anything
```

`cua.agent/__init__.py` stays empty so importing `cua.agent.actions` never loads the model SDK.

## Live target

There is one Dolibarr target per agent while agents work in parallel, so resets don't collide:
`DOLIBARR_PORT` 8085 (A), 8086 (B), 8087 (C), each its own compose project (`cua-a`, `cua-b`, `cua-c`). Until
`target/dolibarr/` exists (A, M1), live tests skip. Tests marked `live` must reset the state they depend on.

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
Tenant B, differential read-back and settle-jitter experiments wait until M7 is green and the owner approves.
