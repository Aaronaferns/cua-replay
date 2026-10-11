#!/usr/bin/env bash
# Spike helper: show ports in use and recent Dolibarr image tags.
docker ps --format '{{.Names}} {{.Ports}}'
echo ---
curl -s 'https://hub.docker.com/v2/repositories/dolibarr/dolibarr/tags?page_size=40&ordering=last_updated' \
  | python3 -c 'import sys, json
d = json.load(sys.stdin)
for t in d["results"]:
    print(t["name"], t["last_updated"][:10])'
