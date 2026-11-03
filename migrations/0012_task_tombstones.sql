-- Expand operation tombstones to cover object-level planner tasks.
-- Additive migration; earlier journal migrations remain unchanged.
ALTER TABLE object_tombstones
    DROP CONSTRAINT object_tombstones_target_kind_check;

ALTER TABLE object_tombstones
    ADD CONSTRAINT object_tombstones_target_kind_check CHECK (
        target_kind IN ('document', 'page', 'object', 'folder', 'task')
    );
