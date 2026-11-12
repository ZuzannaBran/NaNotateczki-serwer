#!/usr/bin/env bash
# Irreversible empty-account activation. Never use this for real account data.
set -Eeuo pipefail
if [[ $# -ne 1 || ! "$1" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
  echo "Usage: $0 <lowercase UUID of a new empty test account>" >&2
  exit 64
fi
owner_id="$1"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
command -v docker >/dev/null || { echo "Docker required" >&2; exit 69; }

# API must be stopped before the irreversible account transition.
# The owner data lock is unavailable to a shell without shared application code.
running="$(docker compose ps --status running --services)" || {
  echo "Cannot verify stopped API; refusing activation." >&2
  exit 69
}
if grep -qx 'api' <<< "$running"; then
  echo "Refusing activation while API is running. Stop api first." >&2
  exit 75
fi

# Only a *completely unused* test owner may be activated. An existing user's
# v1 documents require a separate lossless conversion; never shortcut it.
result="$(
  docker compose exec -T db psql -X -q -A -t -v ON_ERROR_STOP=1 \
    -v owner_id="$owner_id" -U nano_data -d nanotateczki <<'SQL'
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
DO $guard$
DECLARE unknown_tables text;
BEGIN
  SELECT string_agg(c.table_name, ', ' ORDER BY c.table_name)
    INTO unknown_tables
  FROM information_schema.columns c
  WHERE c.table_schema = 'public' AND c.column_name = 'owner_id'
    AND c.table_name NOT IN (
      'refresh_sessions', 'devices', 'device_backups',
      'synced_notebooks', 'change_events', 'idempotency_records',
      'study_shares', 'account_settings', 'planner_entries',
      'planner_entry_mutations', 'document_states', 'document_operations',
      'actor_checkpoints', 'object_tombstones', 'verified_assets',
      'account_sync_protocol'
    );
  IF unknown_tables IS NOT NULL THEN
    RAISE EXCEPTION 'Unknown account tables: %', unknown_tables;
  END IF;
END
$guard$;
INSERT INTO account_sync_protocol (owner_id, protocol)
SELECT users.id, 'v2'
FROM users
WHERE users.id = :'owner_id'::uuid
  AND NOT EXISTS (
      SELECT 1 FROM account_sync_protocol m WHERE m.owner_id = users.id
  )
  AND NOT EXISTS (
      SELECT 1 FROM synced_notebooks n WHERE n.owner_id = users.id
  )
  AND NOT EXISTS (
      SELECT 1 FROM document_states s WHERE s.owner_id = users.id
  )
  AND NOT EXISTS (
      SELECT 1 FROM document_operations o WHERE o.owner_id = users.id
  )
  AND NOT EXISTS (
      SELECT 1 FROM actor_checkpoints cp WHERE cp.owner_id = users.id
  )
  AND NOT EXISTS (
      SELECT 1 FROM object_tombstones t WHERE t.owner_id = users.id
  )
  AND NOT EXISTS (
      SELECT 1 FROM verified_assets a WHERE a.owner_id = users.id
  )
  AND NOT EXISTS (
      SELECT 1 FROM change_events e WHERE e.owner_id = users.id
  )
  AND NOT EXISTS (
      SELECT 1 FROM device_backups b WHERE b.owner_id = users.id
  )
  AND NOT EXISTS (
      SELECT 1 FROM planner_entries p WHERE p.owner_id = users.id
  )
  AND NOT EXISTS (
      SELECT 1 FROM planner_entry_mutations m WHERE m.owner_id = users.id
  )
  AND NOT EXISTS (
      SELECT 1 FROM account_settings a WHERE a.owner_id = users.id
  )
  AND NOT EXISTS (
      SELECT 1 FROM study_shares s WHERE s.owner_id = users.id
  )
  AND NOT EXISTS (
      SELECT 1 FROM idempotency_records i WHERE i.owner_id = users.id
  )
RETURNING owner_id;
COMMIT;
SQL
)"
if [[ "$result" != "$owner_id" ]]; then
  echo "No activation: account not found, already active, or contains data." >&2
  exit 75
fi
echo "Activated EMPTY test account $owner_id for v2 (database marker committed)."
echo "Set OPERATIONS_V2_WRITE_MODE=canary and whitelist this UUID before API start."
