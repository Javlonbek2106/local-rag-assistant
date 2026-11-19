"""The ONLY module of the connector that touches the internal database, and only the connector's own tables.

Ownership rule: every statement that reads or changes a connection, or anything that hangs off one, is tied to
`owner_principal_id`. The owner is always an argument the caller got from the authenticated principal (rag/api.py); it
is never read from a request body. A connection of another owner is indistinguishable from one that does not exist
(None). tests/unit/test_connector_boundaries.py checks that every SQL text of this file mentions the owner.

The credential columns are named in exactly two statements: the insert/update that stores them and `get_sealed`. Every
other statement lists its columns, so a credential cannot reach a list, a view or a response by accident.
"""

import contextlib
import uuid

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from rag import db
from rag.connector import schema_model, snapshots
from rag.connector.errors import ConnectorError, DriverError
from rag.connector.vault import Sealed

# Everything about a connection that may be shown. No credential column, not even the ciphertext.
VIEW = (
    "c.id, c.name, c.description, c.provider, c.host, c.port, c.database_name, c.username, c.ssl_mode, c.options, "
    "c.is_enabled, c.created_at, c.updated_at, c.last_tested_at, c.last_test_status, c.last_test_error, "
    "c.last_test_warning, c.schema_discovered_at, c.credential_key_id, c.last_discovery_at, c.last_discovery_status, "
    "c.last_discovery_error, c.discovery_started_at, c.current_snapshot_id, c.selection_revision, "
    "c.context_confirmed_version"
)
# The columns a person may change by name. The SET clause is built from THIS list, never from request keys.
EDITABLE = ("name", "description", "provider", "host", "port", "database_name", "username", "ssl_mode", "options")
BATCH = 500


def _row(record):
    if record is None:
        return None
    row = dict(record)
    row["id"] = str(row["id"])
    return row


# ---------- connections ----------


def count(owner):
    with db.pool().connection() as conn:
        return conn.execute(
            "SELECT count(*) AS n FROM connections c WHERE c.owner_principal_id = %s", (owner,)
        ).fetchone()["n"]


def list_connections(owner):
    with db.pool().connection() as conn:
        rows = conn.execute(
            f"SELECT {VIEW} FROM connections c WHERE c.owner_principal_id = %s ORDER BY lower(c.name)", (owner,)
        ).fetchall()
    return [_row(r) for r in rows]


def get(owner, connection_id):
    with db.pool().connection() as conn:
        return _row(
            conn.execute(
                f"SELECT {VIEW} FROM connections c WHERE c.owner_principal_id = %s AND c.id = %s",
                (owner, connection_id),
            ).fetchone()
        )


def get_sealed(owner, connection_id):
    """The stored envelope of the credential of one connection of this owner, or None. Used only by the vault path."""
    with db.pool().connection() as conn:
        found = conn.execute(
            "SELECT c.credential_ciphertext, c.credential_nonce, c.credential_key_id, "
            "c.credential_version FROM connections c WHERE c.owner_principal_id = %s AND c.id = %s",
            (owner, connection_id),
        ).fetchone()
    if found is None:
        return None
    return Sealed(
        bytes(found["credential_ciphertext"]),
        bytes(found["credential_nonce"]),
        found["credential_key_id"],
        found["credential_version"],
    )


def all_sealed(owner):
    """[(connection id, Sealed)] of this owner: for `connector verify` and the re-wrap, one row at a time in memory."""
    with db.pool().connection() as conn:
        rows = conn.execute(
            "SELECT c.id, c.credential_ciphertext, c.credential_nonce, c.credential_key_id, "
            "c.credential_version FROM connections c WHERE c.owner_principal_id = %s "
            "ORDER BY c.created_at",
            (owner,),
        ).fetchall()
    return [
        (
            str(r["id"]),
            Sealed(
                bytes(r["credential_ciphertext"]),
                bytes(r["credential_nonce"]),
                r["credential_key_id"],
                r["credential_version"],
            ),
        )
        for r in rows
    ]


def principals():
    """The ids of the principals (for the operator tools that visit every owner in turn; not reachable from the API)."""
    with db.pool().connection() as conn:
        return [str(r["id"]) for r in conn.execute("SELECT id FROM principals ORDER BY created_at").fetchall()]


def replace_sealed(owner, connection_id, sealed):
    """Store a new envelope for the SAME secret (the re-wrap under another key): nothing else about the row changes."""
    with db.pool().connection() as conn:
        done = conn.execute(
            "UPDATE connections c SET credential_ciphertext = %s, credential_nonce = %s, "
            "credential_key_id = %s, credential_version = %s WHERE c.owner_principal_id = %s "
            "AND c.id = %s",
            (sealed.ciphertext, sealed.nonce, sealed.key_id, sealed.version, owner, connection_id),
        )
    return done.rowcount == 1


def insert(owner, connection_id, fields, sealed):
    try:
        with db.pool().connection() as conn:
            conn.execute(
                "INSERT INTO connections (id, owner_principal_id, name, description, provider, host, port, "
                "database_name, username, credential_ciphertext, credential_nonce, credential_key_id, "
                "credential_version, ssl_mode, options) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "%s, %s)",
                (
                    connection_id,
                    owner,
                    fields["name"],
                    fields["description"],
                    fields["provider"],
                    fields["host"],
                    fields["port"],
                    fields["database_name"],
                    fields["username"],
                    sealed.ciphertext,
                    sealed.nonce,
                    sealed.key_id,
                    sealed.version,
                    fields["ssl_mode"],
                    Jsonb(fields["options"]),
                ),
            )
    except UniqueViolation:
        raise ConnectorError("name_taken", 409) from None
    return get(owner, connection_id)


def update(owner, connection_id, changes, sealed=None):
    """Apply `changes` (keys from EDITABLE) and, if given, a new envelope. The row and its schedule of tests are touched
    only by this owner's id; returns the new view, or None when the connection is not this owner's."""
    assert set(changes) <= set(EDITABLE)
    sets, params = [], []
    for column in EDITABLE:
        if column in changes:
            sets.append(f"{column} = %s")
            params.append(Jsonb(changes[column]) if column == "options" else changes[column])
    if sealed is not None:
        sets += [
            "credential_ciphertext = %s",
            "credential_nonce = %s",
            "credential_key_id = %s",
            "credential_version = %s",
        ]
        params += [sealed.ciphertext, sealed.nonce, sealed.key_id, sealed.version]
    if not sets:
        return get(owner, connection_id)
    target_changed = bool(set(changes) - {"name", "description"}) or sealed is not None
    if target_changed:  # what was tested and found is about the OLD target
        sets += [
            "last_tested_at = NULL",
            "last_test_status = NULL",
            "last_test_error = NULL",
            "last_test_warning = NULL",
        ]
    sets.append("updated_at = now()")
    try:
        with db.pool().connection() as conn:
            done = conn.execute(
                f"UPDATE connections c SET {', '.join(sets)} WHERE c.owner_principal_id = %s AND c.id = %s",
                (*params, owner, connection_id),
            )
            if done.rowcount == 0:
                return None
    except UniqueViolation:
        raise ConnectorError("name_taken", 409) from None
    return get(owner, connection_id)


def set_enabled(owner, connection_id, enabled):
    with db.pool().connection() as conn:
        done = conn.execute(
            "UPDATE connections c SET is_enabled = %s, updated_at = now() "
            "WHERE c.owner_principal_id = %s AND c.id = %s",
            (enabled, owner, connection_id),
        )
    return done.rowcount == 1


def delete(owner, connection_id):
    """Delete one connection of this owner; what hangs off it (discovered objects, descriptions) goes with it
    in the same
    statement (ON DELETE CASCADE). True when something was deleted."""
    with db.pool().connection() as conn:
        done = conn.execute(
            "DELETE FROM connections c WHERE c.owner_principal_id = %s AND c.id = %s", (owner, connection_id)
        )
    return done.rowcount == 1


def record_test(owner, connection_id, status, error, warning):
    with db.pool().connection() as conn:
        conn.execute(
            "UPDATE connections c SET last_tested_at = now(), last_test_status = %s, last_test_error = %s, "
            "last_test_warning = %s WHERE c.owner_principal_id = %s AND c.id = %s",
            (status, error, warning, owner, connection_id),
        )


# ---------- discovered schema ----------


class _Unchanged(Exception):
    """The discovery found exactly what the current snapshot says (raised to roll back the rows just written)."""


class SchemaWriter:
    """Receives what a driver finds, one item at a time, and writes it in batches inside one transaction, under one
    snapshot. It holds the objects' ids (at most MAX_OBJECTS of them) and one batch of rows: never the whole schema."""

    def __init__(self, conn, connection_id, snapshot_id, max_objects, max_columns):
        self._conn, self._connection_id, self._snapshot_id = conn, connection_id, snapshot_id
        self._max_objects, self._max_columns = max_objects, max_columns
        self._ids, self._columns_seen, self._constraints_seen, self._indexes_seen = {}, 0, 0, 0
        self._buffers = {"objects": [], "columns": [], "constraints": [], "indexes": []}
        self.result = None  # set when the replacement has finished: what happened (see schema_replacement)

    def object(self, schema, name, kind):
        if len(self._ids) >= self._max_objects:
            raise DriverError("too_large")
        object_id = uuid.uuid4()
        self._ids[(schema, name)] = object_id
        self._push("objects", (object_id, self._connection_id, self._snapshot_id, schema, name, kind))

    def column(self, schema, name, ordinal, column_name, data_type, nullable):
        object_id = self._ids.get((schema, name))
        if object_id is None:
            return
        self._columns_seen += 1
        if self._columns_seen > self._max_columns:
            raise DriverError("too_large")
        self._push("columns", (object_id, ordinal, column_name, data_type, nullable))

    def constraint(
        self, schema, name, kind, constraint_name, columns, ref_schema=None, ref_object=None, ref_columns=None
    ):
        object_id = self._ids.get((schema, name))
        if object_id is not None:
            self._constraints_seen += 1
            self._push(
                "constraints",
                (
                    uuid.uuid4(),
                    object_id,
                    kind,
                    constraint_name,
                    list(columns),
                    ref_schema,
                    ref_object,
                    list(ref_columns) if ref_columns is not None else None,
                ),
            )

    def index(self, schema, name, index_name, columns, unique, primary):
        object_id = self._ids.get((schema, name))
        if object_id is not None:
            self._indexes_seen += 1
            self._push("indexes", (uuid.uuid4(), object_id, index_name, list(columns), unique, primary))

    _SQL = {
        "objects": "INSERT INTO connection_objects (id, connection_id, snapshot_id, schema_name, object_name, "
        "object_type) VALUES (%s, %s, %s, %s, %s, %s)",
        "columns": "INSERT INTO connection_columns (object_id, ordinal, column_name, data_type, is_nullable) "
        "VALUES (%s, %s, %s, %s, %s)",
        "constraints": "INSERT INTO connection_constraints (id, object_id, kind, constraint_name, columns, ref_schema, "
        "ref_object, ref_columns) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        "indexes": "INSERT INTO connection_indexes (id, object_id, index_name, columns, is_unique, is_primary) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
    }

    def _push(self, kind, row):
        self._buffers[kind].append(row)
        if len(self._buffers[kind]) >= BATCH:
            self.flush(kind)

    def flush(self, only=None):
        for kind in ("objects", "columns", "constraints", "indexes"):  # the objects first: the others refer to them
            if only in (None, kind) and self._buffers[kind]:
                if kind != "objects":
                    self.flush("objects")
                with self._conn.cursor() as cursor:
                    cursor.executemany(self._SQL[kind], self._buffers[kind])
                self._buffers[kind] = []

    def counts(self):
        return {"objects": len(self._ids), "columns": self._columns_seen}


@contextlib.contextmanager
def schema_replacement(owner, connection_id, max_objects, max_columns):
    """One transaction, on a connection of its own (not a pooled one: a discovery may take a minute). Inside it the
    rows the driver finds are written through the yielded SchemaWriter under a NEW snapshot. When the body ends:

    - the fingerprint of the new rows is computed; if it equals the current snapshot's, the new rows are rolled back (a
      savepoint) and only the time the current snapshot was confirmed is updated: nothing is duplicated;
    - otherwise the changes against the current snapshot are recorded, the new snapshot becomes the current one, and
      snapshots beyond the retention are removed.

    Any failure, including one raised by the driver halfway, rolls everything back: there is never a snapshot that is
    only part of a discovery, and the current snapshot stays what it was. `writer.result` tells what happened."""
    conn = db.standalone_connection()
    try:
        with conn.transaction():
            owned = conn.execute(
                "SELECT c.provider, c.database_name FROM connections c WHERE c.owner_principal_id = %s AND c.id = %s "
                "FOR UPDATE",
                (owner, connection_id),
            ).fetchone()
            if owned is None:
                raise ConnectorError("not_found", 404)
            previous = snapshots.previous_snapshot(conn, connection_id)
            unchanged = False
            try:
                with conn.transaction():  # a savepoint: the new rows can be thrown away without losing the rest
                    snapshot_id, version = snapshots.new_snapshot(
                        conn, connection_id, owned["provider"], owned["database_name"]
                    )
                    writer = SchemaWriter(conn, connection_id, snapshot_id, max_objects, max_columns)
                    yield writer
                    writer.flush()
                    provider = owned["provider"]
                    fingerprint = snapshots.fingerprint(conn, snapshot_id, provider)
                    if previous is not None:
                        same_algorithm = previous["fingerprint_version"] == schema_model.FINGERPRINT_VERSION
                        before = (
                            previous["fingerprint"]
                            if same_algorithm
                            else snapshots.fingerprint(conn, previous["id"], provider)
                        )
                        if before == fingerprint:
                            raise _Unchanged()
                    found = snapshots.counts(conn, snapshot_id)
                    changes, truncated = 0, False
                    if previous is not None:
                        changes, truncated = snapshots.compute_changes(
                            conn, connection_id, previous["id"], snapshot_id, version
                        )
                    conn.execute(
                        "UPDATE schema_snapshots SET fingerprint = %s, fingerprint_version = %s, objects_count = %s, "
                        "columns_count = %s, constraints_count = %s, indexes_count = %s, changes_count = %s, "
                        "changes_truncated = %s WHERE id = %s",
                        (
                            fingerprint,
                            schema_model.FINGERPRINT_VERSION,
                            found["objects"],
                            found["columns"],
                            found["constraints"],
                            found["indexes"],
                            changes,
                            truncated,
                            snapshot_id,
                        ),
                    )
                    conn.execute(
                        "UPDATE connections c SET current_snapshot_id = %s, "
                        "context_confirmed_version = CASE WHEN c.current_snapshot_id IS NULL THEN %s "
                        "ELSE c.context_confirmed_version END WHERE c.owner_principal_id = %s AND c.id = %s",
                        (snapshot_id, version, owner, connection_id),
                    )
                    writer.result = {
                        "snapshot_id": str(snapshot_id),
                        "version": version,
                        "fingerprint": fingerprint,
                        "changed": previous is not None,
                        "first": previous is None,
                        "changes": changes,
                        "changes_truncated": truncated,
                    }
            except _Unchanged:
                unchanged = True
                writer.result = {
                    "snapshot_id": str(previous["id"]),
                    "version": previous["version"],
                    "fingerprint": before,
                    "changed": False,
                    "first": False,
                    "changes": 0,
                    "changes_truncated": False,
                }
            if unchanged:  # outside the savepoint, which was rolled back: this is kept
                conn.execute(
                    "UPDATE schema_snapshots SET verified_at = now(), fingerprint = %s, fingerprint_version = %s "
                    "WHERE id = %s",
                    (writer.result["fingerprint"], schema_model.FINGERPRINT_VERSION, previous["id"]),
                )
            else:
                snapshots.prune(conn, connection_id)
            conn.execute(
                "UPDATE connections c SET schema_discovered_at = now(), last_discovery_at = now(), "
                "last_discovery_status = 'ok', last_discovery_error = NULL, discovery_started_at = NULL "
                "WHERE c.owner_principal_id = %s AND c.id = %s",
                (owner, connection_id),
            )
    finally:
        conn.close()


def _owned(conn, owner, connection_id):
    return (
        conn.execute(
            "SELECT 1 AS ok FROM connections c WHERE c.owner_principal_id = %s AND c.id = %s", (owner, connection_id)
        ).fetchone()
        is not None
    )


def schemas(owner, connection_id):
    with db.pool().connection() as conn:
        rows = conn.execute(
            "SELECT o.schema_name, count(*) FILTER (WHERE o.object_type = 'table') AS tables, "
            "count(*) FILTER (WHERE o.object_type <> 'table') AS views FROM connection_objects o "
            "JOIN connections c ON c.id = o.connection_id AND o.snapshot_id = c.current_snapshot_id "
            "WHERE c.owner_principal_id = %s AND c.id = %s GROUP BY o.schema_name ORDER BY o.schema_name",
            (owner, connection_id),
        ).fetchall()
    return [dict(r) for r in rows]


def objects(owner, connection_id, schema=None, query=None, limit=100, offset=0):
    """A page of tables and views (with the number of columns); (rows, total)."""
    where, params = "c.owner_principal_id = %s AND c.id = %s", [owner, connection_id]
    if schema:
        where += " AND o.schema_name = %s"
        params.append(schema)
    if query:
        where += " AND o.object_name ILIKE %s ESCAPE '\\'"
        params.append("%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    with db.pool().connection() as conn:
        total = conn.execute(
            "SELECT count(*) AS n FROM connection_objects o JOIN connections c ON c.id = o.connection_id "
            f"AND o.snapshot_id = c.current_snapshot_id WHERE {where}",
            params,
        ).fetchone()["n"]
        rows = conn.execute(
            "SELECT o.schema_name, o.object_name, o.object_type, (SELECT count(*) FROM connection_columns k "
            "WHERE k.object_id = o.id) AS columns FROM connection_objects o "
            "JOIN connections c ON c.id = o.connection_id AND o.snapshot_id = c.current_snapshot_id "
            f"WHERE {where} ORDER BY o.schema_name, o.object_name LIMIT %s OFFSET %s",
            (*params, limit, offset),
        ).fetchall()
    return [dict(r) for r in rows], total


def object_detail(owner, connection_id, schema, name):
    """One table or view with its columns, keys and indexes, or None."""
    with db.pool().connection() as conn:
        found = conn.execute(
            "SELECT o.id, o.schema_name, o.object_name, o.object_type FROM connection_objects o "
            "JOIN connections c ON c.id = o.connection_id AND o.snapshot_id = c.current_snapshot_id "
            "WHERE c.owner_principal_id = %s AND c.id = %s AND o.schema_name = %s AND o.object_name = %s",
            (owner, connection_id, schema, name),
        ).fetchone()
        if found is None:
            return None
        columns = conn.execute(
            "SELECT k.ordinal, k.column_name, k.data_type, k.is_nullable FROM connection_columns k "
            "JOIN connection_objects o ON o.id = k.object_id "
            "JOIN connections c ON c.id = o.connection_id WHERE c.owner_principal_id = %s "
            "AND o.id = %s ORDER BY k.ordinal",
            (owner, found["id"]),
        ).fetchall()
        constraints = conn.execute(
            "SELECT k.kind, k.constraint_name, k.columns, k.ref_schema, k.ref_object, k.ref_columns "
            "FROM connection_constraints k JOIN connection_objects o ON o.id = k.object_id "
            "JOIN connections c ON c.id = o.connection_id WHERE c.owner_principal_id = %s AND o.id = %s "
            "ORDER BY k.kind, k.constraint_name",
            (owner, found["id"]),
        ).fetchall()
        indexes = conn.execute(
            "SELECT k.index_name, k.columns, k.is_unique, k.is_primary FROM connection_indexes k "
            "JOIN connection_objects o ON o.id = k.object_id "
            "JOIN connections c ON c.id = o.connection_id WHERE c.owner_principal_id = %s "
            "AND o.id = %s ORDER BY k.index_name",
            (owner, found["id"]),
        ).fetchall()
    return {
        "schema": found["schema_name"],
        "name": found["object_name"],
        "type": found["object_type"],
        "columns": [dict(r) for r in columns],
        "constraints": [dict(r) for r in constraints],
        "indexes": [dict(r) for r in indexes],
    }


def relationships(owner, connection_id, limit=500, offset=0):
    """Foreign keys of the connection, as (table, columns) -> (referenced table, columns), a page at a time."""
    with db.pool().connection() as conn:
        rows = conn.execute(
            "SELECT o.schema_name, o.object_name, k.constraint_name, k.columns, k.ref_schema, k.ref_object, "
            "k.ref_columns FROM connection_constraints k JOIN connection_objects o ON o.id = k.object_id "
            "JOIN connections c ON c.id = o.connection_id AND o.snapshot_id = c.current_snapshot_id "
            "WHERE c.owner_principal_id = %s AND c.id = %s AND k.kind = 'foreign_key' ORDER BY o.schema_name, "
            "o.object_name, k.constraint_name LIMIT %s OFFSET %s",
            (owner, connection_id, limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- descriptions ----------


ORPHANED = (
    "NOT (d.target_kind = 'database' OR (d.target_kind = 'schema' AND EXISTS (SELECT 1 FROM connection_objects o "
    "WHERE o.snapshot_id = c.current_snapshot_id AND o.schema_name = d.schema_name)) "
    "OR (d.target_kind = 'table' AND EXISTS (SELECT 1 FROM connection_objects o WHERE o.snapshot_id = "
    "c.current_snapshot_id AND o.schema_name = d.schema_name AND o.object_name = d.object_name)) "
    "OR (d.target_kind = 'column' AND EXISTS (SELECT 1 FROM connection_columns k JOIN connection_objects o "
    "ON o.id = k.object_id WHERE o.snapshot_id = c.current_snapshot_id AND o.schema_name = d.schema_name "
    "AND o.object_name = d.object_name AND k.column_name = d.column_name)))"
)


def descriptions(owner, connection_id):
    """The descriptions of a connection, each with `orphaned`: what it describes is not in the current snapshot (it
    stays, and is current again if the object returns)."""
    with db.pool().connection() as conn:
        rows = conn.execute(
            "SELECT d.target_kind, d.schema_name, d.object_name, d.column_name, d.description, d.updated_at, "
            f"{ORPHANED} AS orphaned FROM connection_descriptions d JOIN connections c ON c.id = d.connection_id "
            "WHERE c.owner_principal_id = %s AND c.id = %s ORDER BY d.target_kind, d.schema_name, d.object_name, "
            "d.column_name",
            (owner, connection_id),
        ).fetchall()
    return [dict(r) for r in rows]


def target_exists(owner, connection_id, kind, schema, name, column):
    """Whether what a description is about is in the discovered schema (a database always is, if the connection is)."""
    with db.pool().connection() as conn:
        if not _owned(conn, owner, connection_id):
            return False
        if kind == "database":
            return True
        if kind == "schema":
            sql, params = (
                (
                    "SELECT 1 AS ok FROM connection_objects o JOIN connections c ON c.id = o.connection_id "
                    "AND o.snapshot_id = c.current_snapshot_id WHERE c.id = %s AND o.schema_name = %s LIMIT 1"
                ),
                (connection_id, schema),
            )
        elif kind == "table":
            sql, params = (
                (
                    "SELECT 1 AS ok FROM connection_objects o JOIN connections c ON c.id = o.connection_id "
                    "AND o.snapshot_id = c.current_snapshot_id WHERE c.id = %s AND o.schema_name = %s "
                    "AND o.object_name = %s"
                ),
                (connection_id, schema, name),
            )
        else:
            sql, params = (
                (
                    "SELECT 1 AS ok FROM connection_columns k JOIN connection_objects o ON o.id = k.object_id "
                    "JOIN connections c ON c.id = o.connection_id AND o.snapshot_id = c.current_snapshot_id "
                    "WHERE c.id = %s AND o.schema_name = %s AND o.object_name = %s AND k.column_name = %s"
                ),
                (connection_id, schema, name, column),
            )
        return conn.execute(sql, params).fetchone() is not None


def set_description(owner, connection_id, kind, schema, name, column, text):
    """Store the description of one thing, or delete it when `text` is empty. True if the connection is the owner's."""
    with db.pool().connection() as conn:
        if not _owned(conn, owner, connection_id):
            return False
        with (
            conn.transaction(),
            snapshots.bounded_lock_wait(conn),
        ):  # a running discovery holds the row: `busy`, no wait
            if text:
                conn.execute(
                    "INSERT INTO connection_descriptions (connection_id, target_kind, schema_name, object_name, "
                    "column_name, description) SELECT c.id, %s, %s, %s, %s, %s FROM connections c "
                    "WHERE c.owner_principal_id = %s AND c.id = %s ON CONFLICT (connection_id, target_kind, "
                    "schema_name, object_name, column_name) DO UPDATE SET description = EXCLUDED.description, "
                    "updated_at = now()",
                    (kind, schema, name, column, text, owner, connection_id),
                )
            else:
                conn.execute(
                    "DELETE FROM connection_descriptions d USING connections c WHERE d.connection_id = c.id "
                    "AND c.owner_principal_id = %s AND c.id = %s AND d.target_kind = %s AND d.schema_name = %s "
                    "AND d.object_name = %s AND d.column_name = %s",
                    (owner, connection_id, kind, schema, name, column),
                )
    return True
