#!/usr/bin/env bash
# Irreversibly reset all test account data, backups and PostgreSQL state.
set -Eeuo pipefail
if [[ $# -ne 1 || "$1" != "--delete-all-test-data" ]]; then
  echo "Usage: bash scripts/reset-all-test-data.sh --delete-all-test-data" >&2
  exit 64
fi
root="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$root"
if [[ ! -f compose.yaml || ! -f scripts/apply-migrations.sh ]] ||
   ! grep -qx 'name: nanotateczki' compose.yaml; then
  echo "Unexpected server checkout; refusing reset." >&2
  exit 75
fi
command -v docker >/dev/null || { echo "Docker is required." >&2; exit 69; }
docker compose config --quiet
if [[ -L data || ( -e data && ! -d data ) ]]; then
  echo "Unexpected data path; refusing reset." >&2
  exit 75
fi
# No backup: all accounts, notes, operations and the entire database disappear.
docker compose down --volumes
# Bind-mounted backup payloads and reports are outside the database volume.
if [[ -d data ]]; then
  find data -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
fi
mkdir -p data
docker compose up -d --wait db
bash scripts/apply-migrations.sh
docker compose up -d --build
docker compose ps
printf '%s\n' 'RESET COMPLETE: empty database, no accounts, no server backups.'
printf '%s\n' 'Old SQLite/IndexedDB data on devices must be cleared separately.'
