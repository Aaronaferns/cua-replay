#!/usr/bin/env bash
# Start the spike Dolibarr and wait until the login page answers.
set -euo pipefail
cd "$(dirname "$0")"
start=$(date +%s)
docker compose up -d
for i in $(seq 1 120); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8085/index.php || true)
  if [ "$code" = "200" ]; then
    echo "ready after $(( $(date +%s) - start ))s (HTTP $code)"
    break
  fi
  sleep 3
done
docker compose ps
docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' | grep cua-dolibarr || true
curl -s http://localhost:8085/index.php | grep -io '<title>[^<]*' | head -1
