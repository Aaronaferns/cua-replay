# Spikes

Each spike answers one question that blocks a milestone. Results are recorded here as they are run; a spike is never
marked PASS without the run that shows it. Captured output (`spikes/**/out/`) is git-ignored: it holds Dolibarr demo
data and CSRF tokens.

| Spike | Question | Status | Result / decision |
|---|---|---|---|
| Target (Dolibarr) | Can Dolibarr 23.0.4 reproduce every brief §3.3 condition, verifiable via REST? | PASS (2026-10-11) | See `dolibarr/README.md`. Payment form and validation error not yet exercised (S9). |
| S1 AI snapshot | Do `aria_snapshot(mode="ai")` refs work on Dolibarr pages? | PASS (2026-10-11) | Refs act; stale ref → `count()==0` in 7 ms; pages are 32–39k chars, so observation must be scoped. |
| S9 Payment commit | UI payment works; overpayment refused by Dolibarr; lost-504 leaves exactly one payment | BLOCKED (environment) | — |
| S10 DB reset | Post-seed `mariadb-dump` restore < 15 s with identical REST state | PASS (2026-10-12), thin margin | `target/dolibarr/reset.sh`: dump 8.7 MB / 404 tables in 3.2 s; restore 11.9–14.4 s (≈12 s typical) on a loaded host, REST state identical (`test_reset_restores_known_state`). A plain `mariadb < dump` took 17–22 s; the restore sets dynamic InnoDB globals (`flush_log_at_trx_commit=2`, no unique/FK checks, one commit) that revert on db restart. Fallback if the margin bites: a volume-snapshot restore. |
| S2 Process model | One `asyncio.run()` hosts uvicorn + headed Playwright; capture across full-page posts; request log; clean Ctrl+C | BLOCKED (environment) | — |
| S6 Escape hatches | Do user-opened tabs, `file://` and DevTools escape `context.route`? | PASS (2026-10-12, headless) | `s6_escape_hatches.py`, oracle = the denied server's own hit log. A naive `context.route` lets three things escape: a **WebSocket**, a **server 302 to a denied origin** (Playwright does not route redirect hops) and a **download**. `new_guarded_context` contains all of them: `route.fetch(max_redirects=0)` + `Location` vetting, `route_web_socket`, downloads refused, service workers blocked. `file:`, `data:` and `chrome:` are contained. Popups, iframes, beacons, workers and form posts are contained in both variants. Manual only (a person at a headed browser): Ctrl+T tab, address bar, DevTools, drag-and-drop, Save Page As. |
| S7 Name matching | Normalized-exact matching hits "Current outstanding bill", never "… late"; handles `:`, case, NBSP | PASS (2026-10-11, offline) | `s7_name_match.py`: 9/9 probes over captured names. Rule: NFKC → NBSP→space → drop private-use icon glyphs → collapse whitespace → casefold → strip trailing `:`/`*`; anchored equality. Substring matching would have hit 2 elements for the label. Confirmation on Playwright's own name engine is a resolver test in M3. |

**Environment blocker (2026-10-11).** The WSL2 root filesystem was remounted `emergency_ro` after I/O errors (host
`C:` had 3.2 GB free and the distro VHDX had grown to 12.8 GB). Docker cannot start the target until host space is freed
and WSL restarted. S9, S10, S2 and S6 wait on that.
