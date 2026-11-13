#!/usr/bin/env bash
# Destructive reset of ONE explicitly named test account to an empty v2 account.
# Never execute during an API deployment or on unverified production data.
set -Eeuo pipefail

usage() {
  echo "Usage: $0 <lowercase-account-UUID> <exact-username> WIPE-<UUID>" >&2
  exit 64
}
if [[ $# -ne 3 ]] ||
   [[ ! "$1" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] ||
   [[ ! "$2" =~ ^[A-Za-z0-9_]{3,32}$ ]] ||
   [[ "$3" != "WIPE-$1" ]]; then
  usage
fi
owner_id="$1"
expected_username="$2"
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

command -v docker >/dev/null || { echo "Docker required." >&2; exit 69; }

# Check exit status: a failed Compose query must NOT be mistaken for stopped API.
running="$(docker compose ps --status running --services)" || {
  echo "Could not verify running services; refusing reset." >&2
  exit 69
}
if grep -qx 'api' <<< "$running"; then
  echo "Stop API and all clients before resetting this account." >&2
  exit 75
fi

# Avoid SQL interpolation of user-controlled values: psql quotes variables
# outside dollar-quoted PL/pgSQL and stores them in transaction-local settings.
# Any error rolls back ALL rows and the v2 marker in a single transaction.
result="$(
  docker compose exec -T db psql -X -q -A -t -v ON_ERROR_STOP=1 \
    -v owner_id="$owner_id" -v username="$expected_username" \
    -U nano_data -d nanotateczki <<'SQL'
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
SELECT set_config('nn.stage14_owner', :'owner_id', true);
SELECT set_config('nn.stage14_username', :'username', true);
DO $reset$
DECLARE
  target uuid := current_setting('nn.stage14_owner')::uuid;
  expected text := current_setting('nn.stage14_username');
  actual text;
  unknown_tables text;
BEGIN
  SELECT username INTO actual FROM users WHERE id = target FOR UPDATE;
  IF actual IS NULL OR actual <> expected THEN
    RAISE EXCEPTION 'Account not found or username does not match';
  END IF;
  IF EXISTS (SELECT 1 FROM account_sync_protocol WHERE owner_id = target) THEN
    RAISE EXCEPTION 'Already a v2 account: refusing to destroy v2 operations';
  END IF;

  -- Fail closed if a future migration added another account-owned table.
  SELECT string_agg(c.table_name, ', ' ORDER BY c.table_name)
    INTO unknown_tables
  FROM information_schema.columns c
  WHERE c.table_schema = 'public'
    AND c.column_name = 'owner_id'
    AND c.table_name NOT IN (
      'users', 'refresh_sessions', 'devices', 'device_backups',
      'synced_notebooks', 'change_events', 'idempotency_records',
      'study_shares', 'account_settings', 'planner_entries',
      'planner_entry_mutations', 'document_states',
      'document_operations', 'actor_checkpoints', 'object_tombstones',
      'account_sync_protocol', 'verified_assets', 'legacy_account_cutovers'
    );
  IF unknown_tables IS NOT NULL THEN
    RAISE EXCEPTION 'Unknown account tables: %', unknown_tables;
  END IF;

  -- Children before parents; FK cascades handle share grants.
  DELETE FROM verified_assets WHERE owner_id = target;
  DELETE FROM legacy_account_cutovers WHERE owner_id = target;
  DELETE FROM object_tombstones WHERE owner_id = target;
  DELETE FROM actor_checkpoints WHERE owner_id = target;
  DELETE FROM document_operations WHERE owner_id = target;
  DELETE FROM document_states WHERE owner_id = target;
  DELETE FROM study_shares WHERE owner_id = target;
  DELETE FROM change_events WHERE owner_id = target;
  DELETE FROM idempotency_records WHERE owner_id = target;
  DELETE FROM synced_notebooks WHERE owner_id = target;
  DELETE FROM planner_entry_mutations WHERE owner_id = target;
  DELETE FROM planner_entries WHERE owner_id = target;
  DELETE FROM account_settings WHERE owner_id = target;
  DELETE FROM device_backups WHERE owner_id = target;
  DELETE FROM devices WHERE owner_id = target;
  -- Revoke refresh sessions AND short-lived access tokens/WebSockets.
  DELETE FROM refresh_sessions WHERE owner_id = target;
  UPDATE users SET auth_version = auth_version + 1 WHERE id = target;

  INSERT INTO account_sync_protocol (owner_id, protocol)
    VALUES (target, 'v2');
END
$reset$;
COMMIT;
SELECT 'MIGRATED:' || :'owner_id';
SQL
)" || {
  echo "Reset failed; PostgreSQL transaction rolled back." >&2
  exit 75
}
if [[ "$(printf '%s\n' "$result" | tail -n 1)" != "MIGRATED:$owner_id" ]]; then
  echo "Could not verify the v2 marker." >&2
  exit 75
fi

echo "Reset and activated one account: $owner_id (v2)."
echo "Password hash and username unchanged; all existing tokens invalidated."
echo "Old backup files remain on disk but have no database entries."
echo "Clear this account's legacy client databases/outboxes before reconnecting."
echo "Start the v2 API with OPERATIONS_V2_WRITE_MODE=all."
