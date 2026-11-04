-- Stage 2: files must be checksummed and fsynced before an attach.
CREATE TABLE verified_assets (
    owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    sha256 varchar(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes bigint NOT NULL CHECK (size_bytes BETWEEN 1 AND 67108864),
    media_type varchar(64) NOT NULL CHECK (
        media_type IN ('image/png', 'image/jpeg', 'image/webp', 'application/pdf')
    ),
    verified_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, sha256)
);
GRANT SELECT, INSERT, UPDATE, DELETE ON verified_assets TO nano_app;
