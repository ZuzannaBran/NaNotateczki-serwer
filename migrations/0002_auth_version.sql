ALTER TABLE users
    ADD COLUMN auth_version bigint NOT NULL DEFAULT 1
    CHECK (auth_version >= 1);

ALTER TABLE refresh_sessions
    ADD COLUMN auth_version bigint NOT NULL DEFAULT 1
    CHECK (auth_version >= 1);
