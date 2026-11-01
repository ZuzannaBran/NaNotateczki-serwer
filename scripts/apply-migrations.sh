#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

docker compose exec -T db sh /docker-entrypoint-initdb.d/01-create-app-role.sh

docker compose exec -T db psql -v ON_ERROR_STOP=1 \
  -U nano_data -d nanotateczki <<'SQL'
CREATE TABLE IF NOT EXISTS schema_migrations (
    name text PRIMARY KEY,
    checksum char(64) NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);
SQL

for migration in migrations/*.sql; do
  name="$(basename "$migration")"
  checksum="$(sha256sum "$migration" | cut -d' ' -f1)"
  stored="$(
    printf "SELECT checksum FROM schema_migrations WHERE name = :'migration_name';\n" | \
      docker compose exec -T db psql -XAt -U nano_data -d nanotateczki \
        -v migration_name="$name"
  )"
  if [[ -n "$stored" ]]; then
    if [[ "$stored" != "$checksum" ]]; then
      echo "Applied migration was modified: $name" >&2
      exit 65
    fi
    continue
  fi

  {
    echo 'BEGIN;'
    cat "$migration"
    echo
    echo "INSERT INTO schema_migrations(name, checksum)"
    echo "VALUES (:'migration_name', :'migration_checksum');"
    echo 'COMMIT;'
  } | docker compose exec -T db psql -X -v ON_ERROR_STOP=1 \
    -v migration_name="$name" -v migration_checksum="$checksum" \
    -U nano_data -d nanotateczki
done

docker compose exec -T db psql -v ON_ERROR_STOP=1 \
  -U nano_data -d nanotateczki \
  -c 'REVOKE ALL ON TABLE schema_migrations FROM nano_app'
