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
# Keep an existing public tunnel in the same lifecycle as the database.
# Otherwise `down` leaves an orphan holding the network, and `up` cannot
# restore public access after a successful reset.
services="$(docker compose ps --all --services)"
compose=(docker compose)
reset_marker=.reset-public-tunnel
if [[ -L "$reset_marker" || ( -e "$reset_marker" && ! -f "$reset_marker" ) ]]; then
  echo 'Unexpected reset recovery marker; refusing reset.' >&2
  exit 75
fi
if [[ $'\n'"$services"$'\n' == *$'\ncloudflared\n'* || -f "$reset_marker" ]]; then
  compose+=(-f compose.yaml -f compose.public.yaml)
fi
"${compose[@]}" config --quiet
for storage in data backups; do
  if [[ -L "$storage" || ( -e "$storage" && ! -d "$storage" ) ]]; then
    echo "Unexpected $storage path; refusing reset." >&2
    exit 75
  fi
done
# A failed download/build must be discovered before deleting the database.
"${compose[@]}" build api
if [[ ${#compose[@]} -gt 2 ]]; then
  printf '%s\n' 'restore-public-tunnel' > "$reset_marker"
fi
# No backup: all accounts, notes, operations and the entire database disappear.
"${compose[@]}" down --volumes
# Device payloads AND administrator dumps can contain accounts and notes.
for storage in data backups; do
  if [[ -d "$storage" ]]; then
    find "$storage" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
  fi
done
mkdir -p data
"${compose[@]}" up -d --wait db
bash scripts/apply-migrations.sh
# Verify the reset while the API and the public tunnel are still stopped.
docker compose exec -T db psql -X -v ON_ERROR_STOP=1 \
  -U nano_data -d nanotateczki <<'SQL'
DO $reset$
DECLARE account_table text; remaining bigint;
BEGIN
  FOR account_table IN
    SELECT tablename FROM pg_tables
    WHERE schemaname = 'public' AND tablename <> 'schema_migrations'
  LOOP
    EXECUTE format('SELECT count(*) FROM public.%I', account_table) INTO remaining;
    IF remaining <> 0 THEN
      RAISE EXCEPTION 'Reset left % rows in %', remaining, account_table;
    END IF;
  END LOOP;
END $reset$;
SELECT count(*) AS applied_migrations FROM schema_migrations;
SQL
"${compose[@]}" up -d --no-build
ready=0
for attempt in {1..30}; do
  if docker compose exec -T api python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=2)" \
    >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "$ready" != 1 ]]; then
  echo 'Reset database created, but API is not ready. Check API logs.' >&2
  exit 75
fi
"${compose[@]}" ps
rm -f -- "$reset_marker"
printf '%s\n' 'RESET COMPLETE: empty v2 database, no accounts or server backups.'
printf '%s\n' 'V2 write mode is preserved from Compose configuration.'
printf '%s\n' 'Old SQLite/IndexedDB data on devices must be cleared separately.'
