#!/usr/bin/env bash
# Start the target (compose project cua-<DOLIBARR_PORT>) and wait until the login page answers.
# A cold start with demo data takes about 4 minutes (image start + install + demo load); a warm start about 4 s.
# Usage: up.sh [timeout_seconds]   (default 600). Runs where docker is: in WSL on this Windows machine.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_env.sh"

timeout="${1:-600}"
start=$(date +%s)
compose up -d
echo "waiting for http://localhost:${DOLIBARR_PORT}/index.php (project $PROJECT) ..."
while true; do
  # 200 on the login page only once the install and demo load are finished (before that Dolibarr redirects).
  if [ "$(http_code "$(login_url)")" = "200" ] && curl -s --max-time 5 "$(login_url)" | grep -q 'name="username"'; then
    echo "ready after $(( $(date +%s) - start ))s"
    break
  fi
  if [ $(( $(date +%s) - start )) -ge "$timeout" ]; then
    echo "not ready after ${timeout}s; recent web log:" >&2
    compose logs --tail 30 web >&2 || true
    exit 1
  fi
  sleep 3
done
compose ps
