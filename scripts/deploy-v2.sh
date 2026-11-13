#!/usr/bin/env bash
# Deploy the v2-only API in the existing Compose project. Does not reset data.
set -Eeuo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
if [[ $# -ne 0 || ! -f .env ]]; then
  echo 'Run with no arguments from a configured server checkout (.env required).' >&2
  exit 64
fi
# Compose keeps the configured disabled/canary/all mode, including .env.
docker compose build api
docker compose up -d --wait db
docker compose stop api
bash scripts/apply-migrations.sh
docker compose up -d api caddy
for attempt in {1..30}; do
  if docker compose exec -T api python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=2)" \
    >/dev/null 2>&1; then
    bash scripts/stage13-status.sh
    echo 'V2 API ready. Deploy the matching Flutter client.'
    exit 0
  fi
  sleep 1
done
echo 'V2 API did not become ready. Check docker compose logs api.' >&2
exit 1
