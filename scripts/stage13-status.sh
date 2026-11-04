#!/usr/bin/env bash
# Read-only health and consistency indicators for Stage-13 operations.
set -Eeuo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
docker compose exec -T db psql -X -v ON_ERROR_STOP=1 \
  -U nano_data -d nanotateczki <<'SQL'
SELECT now() AS observed_at,
       to_regclass('public.document_operations') AS operation_log,
       to_regclass('public.document_states') AS materialized_states;
DO $$
BEGIN
  IF to_regclass('public.document_operations') IS NULL
     OR to_regclass('public.document_states') IS NULL
     OR to_regclass('public.actor_checkpoints') IS NULL THEN
    RAISE EXCEPTION 'Required v2 tables are absent';
  END IF;
END $$;
SELECT (SELECT count(*) FROM document_operations) AS committed_operations,
       (SELECT count(*) FROM document_states) AS document_projections,
       (SELECT count(*) FROM object_tombstones) AS tombstones,
       (SELECT count(*) FROM actor_checkpoints) AS actor_checkpoints;
SELECT count(*) AS invalid_actor_checkpoints
FROM actor_checkpoints checkpoint
WHERE NOT EXISTS (
  SELECT 1 FROM document_operations operation
  WHERE operation.owner_id = checkpoint.owner_id
    AND operation.actor_id = checkpoint.actor_id
    AND operation.actor_seq = checkpoint.last_seq
    AND operation.operation_id = checkpoint.last_operation_id
);
SELECT count(*) AS v2_activated_accounts
FROM account_sync_protocol;
SELECT count(*) AS v2_accounts_with_legacy_documents
FROM account_sync_protocol marker
WHERE EXISTS (
  SELECT 1 FROM synced_notebooks legacy
  WHERE legacy.owner_id = marker.owner_id
);
SELECT count(*) AS dual_protocol_documents
FROM document_states projection
JOIN synced_notebooks legacy
  ON legacy.owner_id = projection.owner_id
 AND legacy.uid = projection.document_uid;
DO $
BEGIN
  IF EXISTS (
    SELECT 1 FROM account_sync_protocol marker
    JOIN synced_notebooks legacy ON legacy.owner_id = marker.owner_id
  ) THEN
    RAISE EXCEPTION 'Activated v2 account still has legacy documents';
  END IF;
  IF EXISTS (
    SELECT 1 FROM document_states projection
    JOIN synced_notebooks legacy
      ON legacy.owner_id = projection.owner_id
     AND legacy.uid = projection.document_uid
  ) THEN
    RAISE EXCEPTION 'Same UID is present in both v1 and v2';
  END IF;
  IF EXISTS (
    SELECT 1 FROM actor_checkpoints checkpoint
    WHERE NOT EXISTS (
      SELECT 1 FROM document_operations operation
      WHERE operation.owner_id = checkpoint.owner_id
        AND operation.actor_id = checkpoint.actor_id
        AND operation.actor_seq = checkpoint.last_seq
        AND operation.operation_id = checkpoint.last_operation_id
    )
  ) THEN
    RAISE EXCEPTION 'Actor checkpoint has no matching committed operation';
  END IF;
END $;
SQL
