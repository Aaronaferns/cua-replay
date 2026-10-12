# Dolibarr target

Scripts and helpers for the sandbox target: `up.sh` (start), `reset.sh` (restore the post-seed dump), `captcha.sh`
(toggle the login CAPTCHA), `session.sh` (expire a browser session server-side), `seed.py` (seed), `dolictl.py` /
`livetarget.py` (the same from Python, used by tests).

## Windows/WSL notes

- **One target per checkout and port.** The compose project is `cua-<DOLIBARR_PORT>`, and the project remembers the
  compose file path of the checkout that first started it. Give each checkout its own `DOLIBARR_PORT` in its `.env`
  (for example 8085, 8086, 8087); two checkouts on one port share, and clobber, one database. Every script prints
  `compose project: cua-<port>` and `dolictl.run_script` raises if that is not the project `settings().dolibarr_port`
  names.
- **Environment forwarding.** Docker lives in WSL2 and the scripts run there through `wsl`. Windows environment
  variables do not cross into WSL unless listed in `WSLENV`, so `dolictl` forwards `DOLIBARR_PORT` and `CUA_ENV_FILE`
  (a path, translated with `/p`) when they are set, and `MYSQL_PWD` for SQL. A port set only in the shell
  (`DOLIBARR_PORT=8087 uv run pytest -m live`) therefore reaches the scripts; `.env` values are read inside WSL by
  `_env.sh`, with the environment winning.
- **Containers idle when WSL stops.** When the WSL VM shuts down (idle timeout, or a host restart) the containers
  stop with it, and live tests are skipped as "target not reachable" until the next `up.sh`. Never run `wsl --shutdown`
  while other checkouts' targets are in use.
- **Warm start.** Starting an existing, already-installed project takes about 4 s (`up.sh`); a cold start with demo
  data takes about 4 minutes.
