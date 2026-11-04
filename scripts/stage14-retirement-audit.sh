#!/usr/bin/env bash
# Read-only inventory: this is NOT permission to retire the v1 API.
set -Eeuo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
command -v docker >/dev/null || { echo "Docker is required" >&2; exit 69; }
command -v python3 >/dev/null || { echo "Python 3 is required" >&2; exit 69; }

# One consistent PostgreSQL snapshot. Missing migrations, SQL errors and
# failed Docker commands must fail closed, never report a clean migration.
report="$(
  docker compose exec -T db psql -X -q -A -t -v ON_ERROR_STOP=1 \
    -U nano_data -d nanotateczki <<'SQL'
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SELECT json_build_object(
  'legacy_accounts', (
    SELECT count(*) FROM users owner
    LEFT JOIN account_sync_protocol marker ON marker.owner_id = owner.id
    WHERE marker.owner_id IS NULL
  ),
  'v2_accounts', (SELECT count(*) FROM account_sync_protocol),
  'legacy_notebooks', (SELECT count(*) FROM synced_notebooks),
  'v2_documents', (SELECT count(*) FROM document_states),
  'active_legacy_shares', (
    SELECT count(*) FROM study_shares share
    LEFT JOIN account_sync_protocol marker ON marker.owner_id = share.owner_id
    WHERE marker.owner_id IS NULL
      AND share.revoked_at IS NULL AND share.expires_at > now()
  ),
  'dual_protocol_documents', (
    SELECT count(*) FROM document_states state
    JOIN synced_notebooks legacy ON legacy.owner_id = state.owner_id
      AND legacy.uid = state.document_uid
  ),
  'invalid_actor_checkpoints', (
    SELECT count(*) FROM actor_checkpoints checkpoint
    WHERE NOT EXISTS (
      SELECT 1 FROM document_operations operation
      WHERE operation.owner_id = checkpoint.owner_id
        AND operation.actor_id = checkpoint.actor_id
        AND operation.actor_seq = checkpoint.last_seq
        AND operation.operation_id = checkpoint.last_operation_id
    )
  )
)::text;
COMMIT;
SQL
)"

NN_STAGE14_REPORT="$report" python3 - <<'PY'
import json
import os
import sys

keys = (
    "legacy_accounts",
    "v2_accounts",
    "legacy_notebooks",
    "v2_documents",
    "active_legacy_shares",
    "dual_protocol_documents",
    "invalid_actor_checkpoints",
)
try:
    counts = json.loads(os.environ["NN_STAGE14_REPORT"])
    if not isinstance(counts, dict) or set(counts) != set(keys):
        raise ValueError("unexpected inventory keys")
    if any(type(counts[key]) is not int or counts[key] < 0 for key in keys):
        raise ValueError("invalid inventory count")
except (KeyError, ValueError, TypeError) as exc:
    print(f"Stage 14 inventory invalid: {exc}", file=sys.stderr)
    sys.exit(75)

for key in keys:
    print(f"{key}: {counts[key]}")

blockers = (
    "legacy_accounts",
    "legacy_notebooks",
    "active_legacy_shares",
    "dual_protocol_documents",
    "invalid_actor_checkpoints",
)
if any(counts[key] for key in blockers):
    print("BLOCKED: legacy writes/data or integrity issues remain.", file=sys.stderr)
    sys.exit(75)

print("Server-side inventory is clear, but legacy API retirement is NOT approved.")
print("Client cutover, new-account onboarding, backups and E2E still need sign-off.")
PY
