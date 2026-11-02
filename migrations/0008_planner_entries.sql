-- Calendar rows are independent account-owned records, including deletions.
-- Never remove tombstones during normal synchronization.
CREATE TABLE planner_entries (
    owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    uid varchar(128) NOT NULL,
    content jsonb,
    modified_at timestamptz NOT NULL,
    deleted boolean NOT NULL DEFAULT false,
    PRIMARY KEY (owner_id, uid),
    CHECK ((deleted AND content IS NULL) OR
           (NOT deleted AND content IS NOT NULL))
);
GRANT SELECT, INSERT, UPDATE, DELETE ON planner_entries TO nano_app;
