-- Four-hour password-protected Web view-only sessions.
CREATE TABLE study_shares (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    document_uid varchar(128) NOT NULL,
    title varchar(200) NOT NULL,
    password_hash text NOT NULL,
    revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz
);
CREATE INDEX study_shares_owner_idx ON study_shares(owner_id);

CREATE TABLE study_share_grants (
    id uuid PRIMARY KEY,
    share_id uuid NOT NULL REFERENCES study_shares(id) ON DELETE CASCADE,
    token_hash varchar(64) NOT NULL UNIQUE,
    expires_at timestamptz NOT NULL
);
CREATE INDEX study_share_grants_share_idx ON study_share_grants(share_id);
GRANT SELECT, INSERT, UPDATE, DELETE ON study_shares, study_share_grants TO nano_app;
