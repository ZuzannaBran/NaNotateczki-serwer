-- Stage 11/13: persistent account protocol fence.
-- An absent row denotes the legacy v1 mode. Activation is deliberate.
-- No historical notebook or operation is converted by this migration.
CREATE TABLE account_sync_protocol (
    owner_id uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    protocol text NOT NULL CHECK (protocol = 'v2'),
    activated_at timestamptz NOT NULL DEFAULT now()
);
-- The application role only reads the immutable mode marker.
-- Activation requires the administrative migration procedure.
GRANT SELECT ON account_sync_protocol TO nano_app;
