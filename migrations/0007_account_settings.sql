-- Account-scoped preferences are independent from per-device snapshots.
-- Each setting category is revisioned to prevent stale devices overwriting
-- changes made on another device.
CREATE TABLE account_settings (
    owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    category varchar(32) NOT NULL,
    revision bigint NOT NULL CHECK (revision >= 1),
    content jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, category),
    CHECK (category IN (
        'appPreferences', 'editorPreferences', 'recentColors',
        'backupPreferences'
    ))
);
GRANT SELECT, INSERT, UPDATE, DELETE ON account_settings TO nano_app;
