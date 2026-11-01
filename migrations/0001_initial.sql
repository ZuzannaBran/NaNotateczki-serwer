CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE users (
    id uuid PRIMARY KEY,
    email text NOT NULL UNIQUE,
    password_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE refresh_sessions (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash char(64) NOT NULL UNIQUE,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz
);
CREATE INDEX refresh_sessions_owner_idx ON refresh_sessions(owner_id);

CREATE TABLE devices (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind IN ('web', 'backup')),
    last_seen_at timestamptz NOT NULL,
    UNIQUE (owner_id, id)
);
CREATE INDEX devices_owner_idx ON devices(owner_id);

CREATE TABLE device_backups (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_id uuid NOT NULL,
    storage_key text NOT NULL UNIQUE,
    sha256 char(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes bigint NOT NULL CHECK (size_bytes > 0 AND size_bytes <= 67108864),
    format_version integer NOT NULL CHECK (format_version BETWEEN 1 AND 100),
    status text NOT NULL CHECK (status IN ('pending', 'verified', 'rejected')),
    created_at timestamptz NOT NULL DEFAULT now(),
    verified_at timestamptz,
    FOREIGN KEY (owner_id, device_id)
      REFERENCES devices(owner_id, id) ON DELETE CASCADE
);
CREATE INDEX device_backups_latest_idx
  ON device_backups(owner_id, device_id, created_at DESC);

CREATE TABLE synced_notebooks (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    uid varchar(128) NOT NULL,
    content jsonb,
    revision bigint NOT NULL CHECK (revision >= 1),
    deleted_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (owner_id, uid),
    CHECK ((deleted_at IS NULL AND content IS NOT NULL)
        OR (deleted_at IS NOT NULL AND content IS NULL))
);

CREATE TABLE change_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    entity_uid varchar(128) NOT NULL,
    revision bigint NOT NULL CHECK (revision >= 1),
    deleted boolean NOT NULL DEFAULT false,
    content jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((deleted = false AND content IS NOT NULL)
        OR (deleted = true AND content IS NULL))
);
CREATE INDEX change_events_owner_id_idx ON change_events(owner_id, id);

CREATE TABLE idempotency_records (
    owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key varchar(64) NOT NULL,
    request_hash char(64) NOT NULL,
    response jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, key)
);
CREATE INDEX idempotency_created_idx ON idempotency_records(created_at);

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO nano_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO nano_app;
