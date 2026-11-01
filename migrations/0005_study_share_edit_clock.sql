-- Persist server-received edit ordering for collaborative sessions.
-- Do not modify migration 0004; deployment checks its checksum.
ALTER TABLE study_shares
    ADD COLUMN edit_clock jsonb NOT NULL DEFAULT '{}'::jsonb;
