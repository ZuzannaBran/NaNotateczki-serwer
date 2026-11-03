-- Per-entry server revisions and durable idempotency receipts (stage 8).
-- Older v1 clients keep working until account migration (stage 11).
ALTER TABLE planner_entries
    ADD COLUMN revision bigint NOT NULL DEFAULT 1;
ALTER TABLE planner_entries
    ADD CONSTRAINT planner_entry_positive_revision CHECK (revision > 0);

CREATE TABLE planner_entry_mutations (
    owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    operation_id uuid NOT NULL,
    uid varchar(128) NOT NULL,
    payload_hash char(64) NOT NULL,
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, operation_id)
);
CREATE INDEX planner_entry_mutations_owner_uid
    ON planner_entry_mutations (owner_id, uid);
GRANT SELECT, INSERT, UPDATE, DELETE
    ON planner_entry_mutations TO nano_app;
