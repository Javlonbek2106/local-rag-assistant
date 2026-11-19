-- Schema snapshots, change detection, object selection and metadata freshness, milestone M4.2 (roadmap 6a; docs/connector.md).
-- Additive: new tables and new columns; nothing is dropped except one uniqueness rule that is replaced by a finer one.
--
-- Every successful discovery of an external database that CHANGED something is a snapshot: a numbered, immutable copy of
-- the names, types and keys it found (the rows of connection_objects/columns/constraints/indexes now belong to one
-- snapshot). A discovery that finds exactly what the current snapshot already says makes no new snapshot, it only updates
-- the time it was confirmed. A discovery that fails or is cut off leaves no snapshot at all: it ran in one transaction.
-- Rows of the external database are never stored here, only structure.

CREATE TABLE schema_snapshots (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    connection_id       uuid        NOT NULL REFERENCES connections (id) ON DELETE CASCADE,
    version             integer     NOT NULL CHECK (version >= 1),   -- 1, 2, 3 ... per connection, never reused
    fingerprint         text        NOT NULL,                        -- SHA-256 of the canonical structure (rag/connector/schema_model.py)
    fingerprint_version smallint    NOT NULL DEFAULT 1,              -- the algorithm: 0 = not computed yet (rows of M4.1b)
    provider            text        NOT NULL,
    database_name       text        NOT NULL,                        -- the identity of the database at the time
    created_at          timestamptz NOT NULL DEFAULT now(),          -- when this structure was first seen
    verified_at         timestamptz NOT NULL DEFAULT now(),          -- when a discovery last confirmed it
    objects_count       integer     NOT NULL DEFAULT 0,
    columns_count       integer     NOT NULL DEFAULT 0,
    constraints_count   integer     NOT NULL DEFAULT 0,
    indexes_count       integer     NOT NULL DEFAULT 0,
    changes_count       integer     NOT NULL DEFAULT 0,              -- changes against the snapshot before it
    changes_truncated   boolean     NOT NULL DEFAULT false,          -- more changes than are kept (see config)
    UNIQUE (connection_id, version)
);

ALTER TABLE connections
    ADD COLUMN current_snapshot_id    uuid REFERENCES schema_snapshots (id) ON DELETE SET NULL,
    ADD COLUMN discovery_started_at   timestamptz,                   -- set while a discovery runs (a lease: see freshness)
    ADD COLUMN last_discovery_at      timestamptz,                   -- the last ATTEMPT, whatever its outcome
    ADD COLUMN last_discovery_status  text CHECK (last_discovery_status IN ('ok', 'failed')),
    ADD COLUMN last_discovery_error   text,                          -- a fixed code, never text from the driver
    ADD COLUMN context_confirmed_version integer NOT NULL DEFAULT 0, -- the snapshot version the selection was last reviewed against
    ADD COLUMN selection_revision     integer NOT NULL DEFAULT 0;    -- counts changes of the selection (optimistic concurrency)

ALTER TABLE connection_objects ADD COLUMN snapshot_id uuid REFERENCES schema_snapshots (id) ON DELETE CASCADE;

-- Rows made by M4.1b (one discovery per connection, replaced each time) become snapshot 1 of their connection. The
-- fingerprint of such a snapshot is computed at the next discovery (fingerprint_version 0).
INSERT INTO schema_snapshots (connection_id, version, fingerprint, fingerprint_version, provider, database_name,
                              created_at, verified_at, objects_count, columns_count)
SELECT c.id, 1, '', 0, c.provider, c.database_name, coalesce(c.schema_discovered_at, now()),
       coalesce(c.schema_discovered_at, now()),
       (SELECT count(*) FROM connection_objects o WHERE o.connection_id = c.id),
       (SELECT count(*) FROM connection_columns k JOIN connection_objects o ON o.id = k.object_id
        WHERE o.connection_id = c.id)
FROM connections c
WHERE EXISTS (SELECT 1 FROM connection_objects o WHERE o.connection_id = c.id);

UPDATE connection_objects o SET snapshot_id = s.id FROM schema_snapshots s
WHERE s.connection_id = o.connection_id AND s.version = 1;

UPDATE connections c SET current_snapshot_id = s.id, context_confirmed_version = 1,
       last_discovery_at = c.schema_discovered_at, last_discovery_status = 'ok'
FROM schema_snapshots s WHERE s.connection_id = c.id AND s.version = 1;

ALTER TABLE connection_objects ALTER COLUMN snapshot_id SET NOT NULL;
ALTER TABLE connection_objects DROP CONSTRAINT IF EXISTS connection_objects_connection_id_schema_name_object_name_key;
CREATE UNIQUE INDEX connection_objects_snapshot_name_key ON connection_objects (snapshot_id, schema_name, object_name);
CREATE INDEX connection_objects_connection_idx ON connection_objects (connection_id, snapshot_id);

-- What changed between a snapshot and the one before it. Columns, keys and indexes are listed only for objects that exist
-- in both snapshots (a whole object added or removed is ONE change: its parts are implied). A rename is never guessed:
-- it is a removal and an addition.
CREATE TABLE schema_changes (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    connection_id    uuid        NOT NULL REFERENCES connections (id) ON DELETE CASCADE,
    from_snapshot_id uuid        REFERENCES schema_snapshots (id) ON DELETE SET NULL,
    to_snapshot_id   uuid        NOT NULL REFERENCES schema_snapshots (id) ON DELETE CASCADE,
    to_version       integer     NOT NULL,
    object_type      text        NOT NULL CHECK (object_type IN ('schema', 'table', 'view', 'column', 'constraint',
                                                                 'relationship', 'index')),
    schema_name      text        NOT NULL,
    object_name      text        NOT NULL DEFAULT '',
    sub_name         text        NOT NULL DEFAULT '',                -- the column, constraint or index
    change_type      text        NOT NULL CHECK (change_type IN ('added', 'removed', 'modified')),
    previous         jsonb,
    current          jsonb,
    detected_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX schema_changes_connection_idx ON schema_changes (connection_id, to_version);
CREATE INDEX schema_changes_snapshot_idx ON schema_changes (to_snapshot_id);

-- What the owner chose to show to future AI context. By NAME (schema, table, column), like the descriptions, so that a
-- selection survives a new discovery; what is no longer there is shown as unavailable, never deleted. Rules in
-- rag/connector/selection.py: a schema item includes the objects it has now AND those it gets later; a table item
-- includes (or, with mode 'exclude', removes) one table; a column item includes one column of a table that is not
-- included as a whole, or excludes one column of a table that is.
CREATE TABLE connection_selections (
    connection_id uuid        NOT NULL REFERENCES connections (id) ON DELETE CASCADE,
    kind          text        NOT NULL CHECK (kind IN ('schema', 'table', 'column')),
    schema_name   text        NOT NULL,
    object_name   text        NOT NULL DEFAULT '',
    column_name   text        NOT NULL DEFAULT '',
    mode          text        NOT NULL CHECK (mode IN ('include', 'exclude')),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (connection_id, kind, schema_name, object_name, column_name),
    CONSTRAINT connection_selections_shape CHECK (
        (kind = 'schema' AND schema_name <> '' AND object_name = '' AND column_name = '' AND mode = 'include')
        OR (kind = 'table' AND schema_name <> '' AND object_name <> '' AND column_name = '')
        OR (kind = 'column' AND schema_name <> '' AND object_name <> '' AND column_name <> ''))
);
