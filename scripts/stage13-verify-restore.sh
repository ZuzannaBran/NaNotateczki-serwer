#!/usr/bin/env bash
# Run on a disposable test host, not on the production server.
set -Eeuo pipefail
umask 077

if [[ $# -ne 1 || "$1" != /* ]]; then
  echo "Usage: $0 /absolute/path/to/decrypted/backup-directory" >&2
  exit 64
fi
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
backup="$(realpath -- "$1")"
[[ -d "$backup" ]] || { echo "Backup directory missing" >&2; exit 66; }
command -v docker >/dev/null || { echo "Docker is required" >&2; exit 69; }
command -v python3 >/dev/null || { echo "Python 3 is required" >&2; exit 69; }

work="$(mktemp -d)"
container="nn-stage13-restore-$$-$RANDOM"
cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
  rm -rf -- "$work"
}
trap cleanup EXIT

python3 "$root/scripts/stage13_backup_check.py" "$backup" \
  --restore-assets "$work/assets"

# Network is disabled and the database lives only in ephemeral tmpfs.
# Run on an independent machine with memory to fit the restored database.
docker run -d --name "$container" --network none \
  -e POSTGRES_PASSWORD=stage13-disposable-restore-only \
  --mount "type=bind,src=$backup,dst=/restore,readonly" \
  --tmpfs /var/lib/postgresql/data:rw,nosuid,nodev,size=2g \
  --shm-size=128m postgres:17-alpine >/dev/null
ready=0
for _ in $(seq 1 90); do
  if docker exec "$container" pg_isready -U postgres >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "$ready" != 1 ]]; then
  docker logs --tail 60 "$container" >&2
  echo "Isolated PostgreSQL did not start" >&2
  exit 1
fi
docker exec "$container" pg_restore --list /restore/postgres.dump >/dev/null
docker exec "$container" createdb -U postgres nn_stage13_restore
docker exec "$container" pg_restore -U postgres --no-owner --no-acl \
  --exit-on-error --dbname nn_stage13_restore /restore/postgres.dump
# Both schema and data must be queryable. No writes to the source backup.
docker exec "$container" psql -X -v ON_ERROR_STOP=1 -U postgres \
  -d nn_stage13_restore -c \
  "SELECT COUNT(*) AS restored_accounts FROM users;"
docker exec "$container" psql -X -v ON_ERROR_STOP=1 -U postgres \
  -d nn_stage13_restore -c \
  "SELECT name, checksum FROM schema_migrations ORDER BY name;"
# Compare the restored SQL receipts with actual bytes, not only tar metadata.
if [[ "$(docker exec "$container" psql -X -At -v ON_ERROR_STOP=1 -U postgres \
  -d nn_stage13_restore -c "SELECT to_regclass('public.verified_assets') IS NOT NULL;")" == t ]]; then
  docker exec "$container" psql -X -At -v ON_ERROR_STOP=1 -U postgres \
    -d nn_stage13_restore -c "SELECT owner_id, sha256, size_bytes FROM verified_assets;" \
    > "$work/asset-inventory.txt"
  python3 "$root/scripts/stage13_backup_check.py" \
    --verify-inventory "$work/asset-inventory.txt" --asset-root "$work/assets"
fi
echo "STAGE 13 RESTORE PASS: isolated PostgreSQL and restored asset archive"
