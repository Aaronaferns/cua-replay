# Spike: Dolibarr as the target application (2026-10-11)

Question. Can a real, ready-made back-office app replace the self-built practice app planned earlier (SimCU in
`docs/architecture-review-v2.md`), with every runtime condition in brief §3.3 reproducible on demand and verifiable
against the app's own data?

**Verdict: PASS.** Adopt Dolibarr 23.0.4 (pinned) as the target.

## Setup facts

| Fact | Detail |
|---|---|
| Docker | Engine 29.1.3 + Compose 2.40 were already installed in WSL2 Ubuntu 26.04. The earlier "Docker absent" finding only checked the Windows PATH. |
| Start time | Dolibarr 23.0.4 + MariaDB 11.4 with demo data: cold start, including the image pull, 246 s; warm restart 17 s |
| Memory | About 200–300 MB RAM |
| WSL trap | WSL stops the VM about a minute after the last WSL command exits, and the containers stop with it. Keep a WSL process alive, or set `vmIdleTimeout=-1` in `.wslconfig`. Containers use `restart: unless-stopped`. |
| Windows trap | Python `urllib` to `localhost:8085` was refused (IPv6 first; WSL forwards IPv4). Use `127.0.0.1` for API calls. Chromium handles `localhost`. |
| Windows trap | The cp1252 console can't print Dolibarr's icon-font glyphs. Set `PYTHONIOENCODING=utf-8`. |
| Demo data | 30 third parties; 66 invoices (19 draft, 26 validated-unpaid, 20 paid, 1 abandoned); 6 bank accounts; 24 users, including `noperm` |
| API token | The admin API token is `demokey` (demo data). Spike-only. |

## Perception (Playwright 1.63, Python)

- API surface. `page.aria_snapshot(mode="ai", boxes=, depth=)` and `Locator.normalize()` exist. `get_by_ref` is
  missing; it is 1.64.
- Snapshot size and speed:

  | Page | Characters | Refs | Time |
  |---|---|---|---|
  | Login | 752 | 16 |  |
  | App pages | 32–39k | 475–592 | 50–190 ms |

  Observation must be scoped to the main content area; the left and top menus are most of the size.
- Acting by ref. `page.locator("aria-ref=<ref>")` fills and clicks. A stale ref after navigation gives `count()==0`
  in 7 ms.
- `normalize()` returns ID- or name-based selectors: `#select2-…`, `button[name="button_search_x"]`,
  `#searchselectcombo`. Dolibarr's `name`s are stable.
- Real legacy traits:
  - nested layout tables;
  - label cell + value cell (`cell "Customer Code"` | `CU1212-0005`);
  - icon-only buttons with empty names;
  - select2 widgets;
  - six list-filter textboxes sharing one long help-text accessible name, so role + name is never unique;
  - "Current outstanding bill" vs "Current outstanding bill late", a real substring trap.

## Runtime conditions: reproduced and verified

| Condition | Reproduction | Verification |
|---|---|---|
| Permission denied | `noperm` opens an invoice → "Access denied. You try to access to a page, area or feature … that is not allowed to your user." | UI. The API refuses `noperm` too (403). |
| Not found | Customer list filter `ZZ-NO-SUCH` → `cell "No record found"` | REST |
| Near-miss record | Filter code `CU1910-0002` (doesn't exist) → the only row is Company Corp 2, `CU1910-00021` | REST `LIKE` returns only `CU1910-00021` |
| Confirmation dialog + commit | Validate → `dialog "Validate invoice"`: "Are you sure you want to validate this invoice with the reference FA2902-0037?" → Yes | REST: invoice 216 is `FA2902-0037`, status 1 |
| Commit, then lost response | `route.fetch()` sends the real confirm request (server 200), then `route.fulfill(504)` → browser shows "504 Gateway Time-out" | REST: invoice 217 is `FA2908-0038`, status 1 (committed) |
| Session expiry | Cookies dropped → the same URL renders the login form | UI. State detection must read content, not URLs. |
| Human-only gate | `MAIN_SECURITY_ENABLECAPTCHA=1` → `textbox "Security code"`; scripted login refused: "Bad value for security code. Try again with new value..." | UI |
| Posting oracle | `GET /invoices/{id}/payments` lists each payment with its bank line | REST |

Design consequence. Dolibarr's invoice validation commits by a GET carrying a CSRF token
(`action=confirm_valid&confirm=yes&token=…`), so HTTP method alone can't identify a commit. S9 later found that the
payment confirm is a POST with `action=confirm_paiement` in the form body, so the profile's commit routes
(`apps/dolibarr/profile.json`) match method, path, query and body.

Not exercised here: the payment form and a validation-error message such as a payment above the remaining amount
(both covered later by S9), and tenant B, a second instance with translation overrides (cut, REPORT §7).

## Files

- Instance: `docker-compose.yml`, `up.sh`
- Probes: `probe_env.sh`, `api_probe.py`, `api_setup.py` (REST)
- Spikes: `s1_snapshot.py` (perception), `s2_flows.py` (runtime conditions), `s3_search_payment.py` (near-miss,
  payments), `s4_captcha.py`, `captcha.sh` (`on`/`off`)
- Output: `out/` holds snapshots of synthetic demo data (do not commit)

Stop the instance: `wsl -d Ubuntu -- docker compose -f "<path>/docker-compose.yml" down`. Add `-v` to also delete
the data.
