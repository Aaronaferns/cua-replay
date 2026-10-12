#!/usr/bin/env bash
# Sourced by up.sh / reset.sh / captcha.sh. Loads the repo .env (the same values cua.config reads; variables already in
# the environment win, as in cua.config), names the compose project after the port and defines the docker helpers.
# Never echoes a value.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${CUA_ENV_FILE:-$HERE/../../.env}"
if [ ! -f "$ENV_FILE" ]; then
  echo "no .env at $ENV_FILE (copy .env.example and fill it in)" >&2
  exit 2
fi
while IFS= read -r line || [ -n "$line" ]; do
  line="${line%$'\r'}"
  case "$line" in '' | '#'*) continue ;; esac
  key="${line%%=*}"
  val="${line#*=}"
  case "$key" in [A-Za-z_]*) ;; *) continue ;; esac
  val="${val%\"}"; val="${val#\"}"; val="${val%\'}"; val="${val#\'}"
  if [ -z "${!key+x}" ]; then export "$key=$val"; fi
done < "$ENV_FILE"

: "${DOLIBARR_PORT:?set DOLIBARR_PORT in .env}"
: "${DOLIBARR_DB_ROOT_PASSWORD:?set DOLIBARR_DB_ROOT_PASSWORD in .env}"
PROJECT="cua-${DOLIBARR_PORT}"
echo "compose project: $PROJECT"  # dolictl.run_script checks this against settings().dolibarr_port
DUMP_DIR="$HERE/dumps"
DUMP_FILE="$DUMP_DIR/post-seed.sql"

compose() { docker compose -p "$PROJECT" -f "$HERE/docker-compose.yml" "$@"; }

# mariadb client as root inside the db container; the password travels in MYSQL_PWD, not on a command line.
db_root() { compose exec -T -e MYSQL_PWD="$DOLIBARR_DB_ROOT_PASSWORD" db mariadb -uroot dolidb "$@"; }

http_code() { curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$1" || true; }
login_url() { echo "http://127.0.0.1:${DOLIBARR_PORT}/index.php"; }
