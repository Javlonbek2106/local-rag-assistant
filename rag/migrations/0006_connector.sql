-- External database connector, milestone M4.1b (roadmap 6a, ADR-P12..P16): the connections, the credentials (encrypted),
-- and the schema metadata discovered from each external database, with the descriptions people write for it.
-- Additive only: new tables; no existing table or row is touched.
--
-- Nothing here ever holds data rows of an external database: only names, types and keys (decision of 6a.4).
-- The password of a connection is NEVER stored as text: credential_ciphertext is AES-256-GCM output, authenticated with
-- the associated data "connection id + key id" (not the owner: the owner may change, the ciphertext must still open).
-- The key itself lives outside PostgreSQL, the backups and the repository (rag/connector/vault.py).

CREATE TABLE connections (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_principal_id    uuid        NOT NULL REFERENCES principals (id),
    name                  text        NOT NULL CHECK (char_length(name) BETWEEN 1 AND 100),
    description           text        NOT NULL DEFAULT '' CHECK (char_length(description) <= 2000),
    provider              text        NOT NULL CHECK (provider IN ('postgresql', 'mssql')),
    host                  text        NOT NULL CHECK (char_length(host) BETWEEN 1 AND 253),
    port                  integer     NOT NULL CHECK (port BETWEEN 1 AND 65535),
    database_name         text        NOT NULL CHECK (char_length(database_name) BETWEEN 1 AND 128),
    username              text        NOT NULL CHECK (char_length(username) BETWEEN 1 AND 128),
    credential_ciphertext bytea       NOT NULL,                   -- ciphertext + GCM tag
    credential_nonce      bytea       NOT NULL CHECK (octet_length(credential_nonce) = 12),
    credential_key_id     text        NOT NULL,                   -- which key sealed it (key rotation)
    credential_version    smallint    NOT NULL DEFAULT 1,         -- the envelope format
    ssl_mode              text        NOT NULL CHECK (ssl_mode IN ('disable', 'require', 'verify-ca', 'verify-full')),
    options               jsonb       NOT NULL DEFAULT '{}'::jsonb,
    is_enabled            boolean     NOT NULL DEFAULT true,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    last_tested_at        timestamptz,
    last_test_status      text        CHECK (last_test_status IN ('ok', 'failed')),
    last_test_error       text,                                   -- a fixed code, never text from the driver
    last_test_warning     text,                                   -- a fixed code, e.g. the account can write
    schema_discovered_at  timestamptz,
    CONSTRAINT connections_test_consistent CHECK ((last_tested_at IS NULL) = (last_test_status IS NULL))
);

-- A name is unique for its owner; every list and every lookup filters by the owner first.
CREATE UNIQUE INDEX connections_owner_name_key ON connections (owner_principal_id, lower(name));

-- What discovery found. Replaced as a whole (in one transaction) by each discovery; descriptions are kept apart so that they
-- survive it.
CREATE TABLE connection_objects (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    connection_id uuid NOT NULL REFERENCES connections (id) ON DELETE CASCADE,
    schema_name   text NOT NULL,
    object_name   text NOT NULL,
    object_type   text NOT NULL CHECK (object_type IN ('table', 'view', 'materialized_view')),
    UNIQUE (connection_id, schema_name, object_name)
);

CREATE TABLE connection_columns (
    object_id   uuid     NOT NULL REFERENCES connection_objects (id) ON DELETE CASCADE,
    ordinal     integer  NOT NULL,
    column_name text     NOT NULL,
    data_type   text     NOT NULL,
    is_nullable boolean  NOT NULL,
    PRIMARY KEY (object_id, ordinal)
);

-- Primary, unique and foreign keys. A key over several columns is ONE row (columns in key order); a foreign key names the
-- table it points to (by schema and name: the referenced table is in the same connection, discovered in the same pass).
CREATE TABLE connection_constraints (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    object_id       uuid   NOT NULL REFERENCES connection_objects (id) ON DELETE CASCADE,
    kind            text   NOT NULL CHECK (kind IN ('primary_key', 'unique', 'foreign_key')),
    constraint_name text   NOT NULL,
    columns         text[] NOT NULL,
    ref_schema      text,
    ref_object      text,
    ref_columns     text[],
    CONSTRAINT connection_constraints_fk_shape CHECK (
        (kind = 'foreign_key') = (ref_schema IS NOT NULL AND ref_object IS NOT NULL AND ref_columns IS NOT NULL))
);
CREATE INDEX connection_constraints_object_idx ON connection_constraints (object_id);

CREATE TABLE connection_indexes (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    object_id  uuid    NOT NULL REFERENCES connection_objects (id) ON DELETE CASCADE,
    index_name text    NOT NULL,
    columns    text[]  NOT NULL,
    is_unique  boolean NOT NULL,
    is_primary boolean NOT NULL
);
CREATE INDEX connection_indexes_object_idx ON connection_indexes (object_id);

-- What people write about the database, a schema, a table/view or a column. Kept by NAME (not by the id of a discovered row)
-- so that a new discovery does not lose it. Empty strings stand for "not applicable" so that the key stays unique.
CREATE TABLE connection_descriptions (
    connection_id uuid        NOT NULL REFERENCES connections (id) ON DELETE CASCADE,
    target_kind   text        NOT NULL CHECK (target_kind IN ('database', 'schema', 'table', 'column')),
    schema_name   text        NOT NULL DEFAULT '',
    object_name   text        NOT NULL DEFAULT '',
    column_name   text        NOT NULL DEFAULT '',
    description   text        NOT NULL CHECK (char_length(description) BETWEEN 1 AND 4000),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (connection_id, target_kind, schema_name, object_name, column_name),
    CONSTRAINT connection_descriptions_shape CHECK (
        (target_kind = 'database' AND schema_name = '' AND object_name = '' AND column_name = '')
        OR (target_kind = 'schema' AND schema_name <> '' AND object_name = '' AND column_name = '')
        OR (target_kind = 'table' AND schema_name <> '' AND object_name <> '' AND column_name = '')
        OR (target_kind = 'column' AND schema_name <> '' AND object_name <> '' AND column_name <> ''))
);
