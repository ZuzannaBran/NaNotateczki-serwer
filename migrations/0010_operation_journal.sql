-- Stage 4: append-only operation log and independently materialized v2 state.
-- Do not change historical migrations: existing v1 clients keep their tables.
CREATE TABLE document_states (
    owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    document_uid varchar(128) NOT NULL,
    revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
    content jsonb,
    deleted_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, document_uid),
    CHECK ((deleted_at IS NULL AND content IS NOT NULL)
        OR (deleted_at IS NOT NULL AND content IS NULL))
);

CREATE TABLE actor_checkpoints (
    owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    actor_id varchar(128) NOT NULL,
    principal_kind varchar(8) NOT NULL CHECK (principal_kind IN ('owner', 'share')),
    principal_id uuid NOT NULL,
    share_id uuid, -- immutable actor binding survives share-row deletion
    last_seq bigint NOT NULL CHECK (last_seq BETWEEN 1 AND 9007199254740991),
    last_operation_id uuid NOT NULL,
    last_logical_clock bigint NOT NULL CHECK (
        last_logical_clock BETWEEN 1 AND 9007199254740991
    ),
    PRIMARY KEY (owner_id, actor_id),
    CHECK ((principal_kind = 'owner' AND share_id IS NULL)
        OR (principal_kind = 'share' AND share_id IS NOT NULL))
);

CREATE TABLE document_operations (
    cursor bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    operation_id uuid NOT NULL UNIQUE,
    actor_id varchar(128) NOT NULL,
    actor_seq bigint NOT NULL CHECK (
        actor_seq BETWEEN 1 AND 9007199254740991
    ),
    document_uid varchar(128) NOT NULL,
    op_kind varchar(40) NOT NULL,
    logical_clock bigint NOT NULL CHECK (
        logical_clock BETWEEN 1 AND 9007199254740991
    ),
    envelope jsonb NOT NULL,
    accepted_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (owner_id, actor_id, actor_seq),
    UNIQUE (owner_id, operation_id)
);
CREATE INDEX document_operations_owner_cursor_idx
    ON document_operations(owner_id, cursor);
CREATE INDEX document_operations_document_cursor_idx
    ON document_operations(owner_id, document_uid, cursor);

CREATE TABLE object_tombstones (
    owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    document_uid varchar(128) NOT NULL,
    target_kind varchar(16) NOT NULL CHECK (
        target_kind IN ('document', 'page', 'object', 'folder')
    ),
    page_id varchar(128) NOT NULL DEFAULT '',
    object_id varchar(128) NOT NULL DEFAULT '',
    deleted_by uuid NOT NULL,
    deleted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, document_uid, target_kind, page_id, object_id),
    FOREIGN KEY (owner_id, deleted_by)
        REFERENCES document_operations(owner_id, operation_id) ON DELETE CASCADE
);
CREATE INDEX object_tombstones_document_idx
    ON object_tombstones(owner_id, document_uid);

GRANT SELECT, INSERT, UPDATE, DELETE ON
    document_states, actor_checkpoints, document_operations, object_tombstones
    TO nano_app;
GRANT USAGE, SELECT ON SEQUENCE document_operations_cursor_seq TO nano_app;
