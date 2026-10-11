# Spikes

Each spike answers one question that blocks a milestone. Results are recorded here as they are run; a spike is never
marked PASS without the run that shows it. Captured output (`spikes/**/out/`) is git-ignored: it holds Dolibarr demo
data and CSRF tokens.

| Spike | Question | Status | Result / decision |
|---|---|---|---|
| Target (Dolibarr) | Can Dolibarr 23.0.4 reproduce every brief §3.3 condition, verifiable via REST? | PASS (2026-10-11) | See `dolibarr/README.md`. Payment form and validation error not yet exercised (S9). |
| S1 AI snapshot | Do `aria_snapshot(mode="ai")` refs work on Dolibarr pages? | PASS (2026-10-11) | Refs act; stale ref → `count()==0` in 7 ms; pages are 32–39k chars, so observation must be scoped. |
| S9 Payment commit | UI payment works; overpayment refused by Dolibarr; lost-504 leaves exactly one payment | BLOCKED (environment) | — |
| S10 DB reset | Post-seed `mariadb-dump` restore < 15 s with identical REST state | BLOCKED (environment) | — |
| S2 Process model | One `asyncio.run()` hosts uvicorn + headed Playwright; capture across full-page posts; request log; clean Ctrl+C | BLOCKED (environment) | — |
| S6 Escape hatches | Do user-opened tabs, `file://` and DevTools escape `context.route`? | BLOCKED (environment) | — |
| S7 Name matching | Normalized-exact matching hits "Current outstanding bill", never "… late"; handles `:`, case, NBSP | PASS (2026-10-11, offline) | `s7_name_match.py`: 9/9 probes over captured names. Rule: NFKC → NBSP→space → drop private-use icon glyphs → collapse whitespace → casefold → strip trailing `:`/`*`; anchored equality. Substring matching would have hit 2 elements for the label. Confirmation on Playwright's own name engine is a resolver test in M3. |

**Environment blocker (2026-10-11).** The WSL2 root filesystem was remounted `emergency_ro` after I/O errors (host
`C:` had 3.2 GB free and the distro VHDX had grown to 12.8 GB). Docker cannot start the target until host space is freed
and WSL restarted. S9, S10, S2 and S6 wait on that.
