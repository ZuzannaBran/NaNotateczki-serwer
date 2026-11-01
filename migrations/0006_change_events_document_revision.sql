-- Historical share bases must be resolved without scanning all account events.
CREATE INDEX change_events_document_revision_idx
    ON change_events(owner_id, entity_uid, revision, id DESC)
    WHERE deleted = false;
