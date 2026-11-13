-- Snapshot history is read-only for the API. The offline importer still
-- reads it as administrator and only writes to the v2 journal.
REVOKE INSERT, UPDATE, DELETE ON synced_notebooks, change_events,
    idempotency_records FROM nano_app;

-- Enable already empty accounts. Accounts with historical content remain
-- archived until explicitly converted or reset, never silently discarded.
INSERT INTO account_sync_protocol (owner_id, protocol)
SELECT u.id, 'v2'
FROM users u
WHERE NOT EXISTS (SELECT 1 FROM synced_notebooks WHERE owner_id = u.id)
  AND NOT EXISTS (SELECT 1 FROM change_events WHERE owner_id = u.id)
  AND NOT EXISTS (SELECT 1 FROM idempotency_records WHERE owner_id = u.id)
  AND NOT EXISTS (
      SELECT 1 FROM device_backups
      WHERE owner_id = u.id AND format_version <> 3
  )
  AND NOT EXISTS (
      SELECT 1 FROM account_settings
      WHERE owner_id = u.id AND category = 'libraryState'
  )
  AND NOT EXISTS (
      SELECT 1 FROM study_shares
      WHERE owner_id = u.id AND COALESCE(edit_clock ->> '_protocol', '') <> 'v2'
  )
ON CONFLICT (owner_id) DO NOTHING;

-- A freshly inserted account has no legacy rows. Assign its protocol in
-- the same transaction without letting the API modify existing markers.
CREATE FUNCTION public.activate_new_v2_account() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
BEGIN
    INSERT INTO public.account_sync_protocol (owner_id, protocol)
    VALUES (NEW.id, 'v2');
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION public.activate_new_v2_account() FROM PUBLIC;
CREATE TRIGGER activate_new_v2_account
AFTER INSERT ON public.users
FOR EACH ROW EXECUTE FUNCTION public.activate_new_v2_account();
