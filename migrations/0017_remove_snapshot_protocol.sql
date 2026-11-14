-- Retire empty snapshot storage without silently discarding account data.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM users u WHERE NOT EXISTS (
        SELECT 1 FROM account_sync_protocol p
        WHERE p.owner_id = u.id AND p.protocol = 'v2'
    )) OR EXISTS (SELECT 1 FROM synced_notebooks)
       OR EXISTS (SELECT 1 FROM change_events)
       OR EXISTS (SELECT 1 FROM idempotency_records)
       OR EXISTS (SELECT 1 FROM study_shares WHERE COALESCE(edit_clock ->> '_protocol', '') <> 'v2')
       OR EXISTS (SELECT 1 FROM device_backups WHERE format_version <> 3)
       OR EXISTS (SELECT 1 FROM account_settings WHERE category = 'libraryState') THEN
        RAISE EXCEPTION 'Snapshot data remains; explicitly migrate or reset it before retirement';
    END IF;
END $$;
DROP TABLE legacy_account_cutovers;
DROP TABLE idempotency_records;
DROP TABLE change_events;
DROP TABLE synced_notebooks;
ALTER TABLE device_backups ADD CONSTRAINT backups_operations_only CHECK (format_version = 3);
ALTER TABLE account_settings ADD CONSTRAINT settings_no_snapshot_library CHECK (category <> 'libraryState');
ALTER TABLE study_shares ADD CONSTRAINT shares_operations_only CHECK (edit_clock ->> '_protocol' = 'v2' AND edit_clock ? '_protocol');
