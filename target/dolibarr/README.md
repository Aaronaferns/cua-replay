# Dolibarr target

Scripts and helpers for the sandbox target: `docker-compose.yml` (Dolibarr and MariaDB), `_env.sh` (shared setup),
`up.sh` (start), `reset.sh` (restore the post-seed dump; `reset.sh dump` writes it to the git-ignored `dumps/`),
`captcha.sh` (toggle the login CAPTCHA), `session.sh` (expire a browser session server-side), `seed.py` (create the
users, customers and invoices in `fixtures.json`), `dolirest.py` (REST client and ground-truth reads), `dolictl.py` /
`livetarget.py` (the scripts from Python, used by tests).

Normally you don't call these directly: `uv run cua demo up` starts the target, waits until it is healthy, restores the
seeded baseline (on the first run it seeds the data and saves the dump instead) and prints the URL. The scripts are for
debugging and for the tests.

## Notes

- One target per checkout and port. The compose project is `cua-<DOLIBARR_PORT>`, and the project remembers the
  compose file path of the checkout that first started it. Give each checkout its own `DOLIBARR_PORT` in its `.env`
  (for example 8085, 8086, 8087); two checkouts on one port share, and clobber, one database. Every script prints
  `compose project: cua-<port>` and `dolictl.run_script` raises if that is not the project `settings().dolibarr_port`
  names.
- Environment forwarding. On Windows, Docker lives in WSL2 and the scripts run there through `wsl`. Windows environment
  variables do not cross into WSL unless listed in `WSLENV`, so `dolictl` forwards `DOLIBARR_PORT` and `CUA_ENV_FILE`
  (a path, translated with `/p`) when they are set, and `MYSQL_PWD` for SQL. A port set only in the shell
  (`DOLIBARR_PORT=8087 uv run pytest -m live`) therefore reaches the scripts; `.env` values are read inside WSL by
  `_env.sh`, with the environment winning.
- On Windows the containers stop when the WSL VM shuts down (idle timeout or a host restart), and live tests skip as
  "target not reachable" until the next `uv run cua demo up`. Never run `wsl --shutdown` while other checkouts' targets
  are in use.
- Start time. Starting an existing, already-installed project takes about 4 s (`up.sh`); `cua demo up` adds the reset,
  about 20 s in all. A cold start (image pull, install and demo data) takes 3 to 4 minutes.
