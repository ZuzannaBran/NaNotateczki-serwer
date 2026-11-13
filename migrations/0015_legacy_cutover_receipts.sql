-- Durable conversion receipt. Archived v1 rows are kept in place and fenced.
-- Only an offline administrative role can insert a receipt or activate v2.
CREATE TABLE legacy_account_cutovers (
    owner_id uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    legacy_cursor bigint NOT NULL CHECK (legacy_cursor >= 0),
    operation_cursor bigint NOT NULL CHECK (operation_cursor >= 0),
    converted_documents integer NOT NULL CHECK (converted_documents >= 0),
    activated_at timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT ON legacy_account_cutovers TO nano_app;
