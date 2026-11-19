"""Schema snapshots, their fingerprints, the changes between them, the owner's selection, and the facts that freshness
and the context are made from (roadmap M4.2). The second module, with repository.py, that touches the internal database.

Ownership: every function that starts from a connection id takes `owner` and filters by `owner_principal_id`. The few
functions that take a `conn` and a snapshot id (fingerprint, changes, pruning) run only inside the transaction of
`repository.schema_replacement`, after that transaction has locked the connection row of its owner; tests/unit/
test_connector_boundaries.py names them.
"""

import contextlib

from psycopg.errors import LockNotAvailable

from rag import config, db
from rag.connector import schema_model
from rag.connector.errors import ConnectorError

LOCK_WAIT = "5s"

CHANGE_ORDER = (
    "array_position(ARRAY['schema','table','view','relationship','constraint','column','index'], object_type)"
)


@contextlib.contextmanager
def bounded_lock_wait(conn):
    """For a short change of a connection (a description, a selection, an acknowledgement), inside its transaction: wait
    at most LOCK_WAIT for a row another transaction holds, then say `busy`. A discovery holds the row of its connection
    for as long as it runs (up to the discovery timeout), and a change that waited for it would hold a pooled
    connection all that time."""
    conn.execute(f"SET LOCAL lock_timeout = '{LOCK_WAIT}'")
    try:
        yield
    except LockNotAvailable:
        raise ConnectorError("busy", 409) from None


def _id(value):
    return None if value is None else str(value)


# ---------- the discovery's own bookkeeping ----------


def begin_discovery(owner, connection_id):
    """Say that a discovery has started (a lease: a process that dies stops counting after the lease, see freshness)."""
    with db.pool().connection() as conn:
        done = conn.execute(
            "UPDATE connections c SET discovery_started_at = now() WHERE c.owner_principal_id = %s AND c.id = %s",
            (owner, connection_id),
        )
    return done.rowcount == 1


def record_failure(owner, connection_id, code):
    """A discovery did not finish: only the fact and a fixed code are kept. The snapshots are untouched."""
    with db.pool().connection() as conn:
        conn.execute(
            "UPDATE connections c SET last_discovery_at = now(), last_discovery_status = 'failed', "
            "last_discovery_error = %s, discovery_started_at = NULL WHERE c.owner_principal_id = %s AND c.id = %s",
            (code, owner, connection_id),
        )


# ---------- inside the discovery's transaction ----------


def previous_snapshot(conn, connection_id):
    """The current snapshot of the (already locked) connection as a dict, or None."""
    return conn.execute(
        "SELECT s.id, s.version, s.fingerprint, s.fingerprint_version FROM schema_snapshots s "
        "JOIN connections c ON c.current_snapshot_id = s.id WHERE c.id = %s",
        (connection_id,),
    ).fetchone()


def new_snapshot(conn, connection_id, provider, database_name):
    """Make the (not yet final) snapshot rows will be written under. Returns (id, version)."""
    version = conn.execute(
        "SELECT coalesce(max(version), 0) + 1 AS v FROM schema_snapshots WHERE connection_id = %s", (connection_id,)
    ).fetchone()["v"]
    row = conn.execute(
        "INSERT INTO schema_snapshots (connection_id, version, fingerprint, provider, database_name) "
        "VALUES (%s, %s, '', %s, %s) RETURNING id",
        (connection_id, version, provider, database_name),
    ).fetchone()
    return row["id"], version


FINGERPRINT_QUERIES = (
    (
        "SELECT o.schema_name, o.object_name, o.object_type FROM connection_objects o WHERE o.snapshot_id = %s "
        'ORDER BY o.schema_name COLLATE "C", o.object_name COLLATE "C"',
        lambda r: schema_model.object_record(r["schema_name"], r["object_name"], r["object_type"]),
    ),
    (
        "SELECT o.schema_name, o.object_name, k.column_name, k.data_type, k.is_nullable FROM connection_columns k "
        'JOIN connection_objects o ON o.id = k.object_id WHERE o.snapshot_id = %s ORDER BY o.schema_name COLLATE "C", '
        'o.object_name COLLATE "C", k.column_name COLLATE "C"',
        lambda r: schema_model.column_record(
            r["schema_name"], r["object_name"], r["column_name"], r["data_type"], r["is_nullable"]
        ),
    ),
    (
        "SELECT o.schema_name, o.object_name, k.kind, k.constraint_name, k.columns, k.ref_schema, k.ref_object, "
        "k.ref_columns FROM connection_constraints k JOIN connection_objects o ON o.id = k.object_id "
        'WHERE o.snapshot_id = %s ORDER BY o.schema_name COLLATE "C", o.object_name COLLATE "C", '
        "k.kind COLLATE \"C\", (CASE WHEN k.kind = 'primary_key' THEN '' ELSE k.constraint_name END) COLLATE \"C\"",
        lambda r: schema_model.constraint_record(
            r["schema_name"],
            r["object_name"],
            r["kind"],
            r["constraint_name"],
            r["columns"],
            r["ref_schema"],
            r["ref_object"],
            r["ref_columns"],
        ),
    ),
    (
        "SELECT o.schema_name, o.object_name, i.index_name, i.columns, i.is_unique, i.is_primary FROM "
        "connection_indexes i "
        'JOIN connection_objects o ON o.id = i.object_id WHERE o.snapshot_id = %s ORDER BY o.schema_name COLLATE "C", '
        'o.object_name COLLATE "C", (CASE WHEN i.is_primary THEN \'\' ELSE i.index_name END) COLLATE "C"',
        lambda r: schema_model.index_record(
            r["schema_name"], r["object_name"], r["index_name"], r["columns"], r["is_unique"], r["is_primary"]
        ),
    ),
)


def fingerprint(conn, snapshot_id, provider):
    """The fingerprint of a stored snapshot, streamed from the database in canonical order (schema_model.py)."""
    fingerprinter = schema_model.Fingerprinter(provider)
    for number, (sql, make) in enumerate(FINGERPRINT_QUERIES):
        with conn.cursor(name=f"fp_{number}") as cursor:
            cursor.itersize = 2000
            cursor.execute(sql, (snapshot_id,))
            for row in cursor:
                fingerprinter.add(make(row))
    return fingerprinter.hexdigest()


def counts(conn, snapshot_id):
    row = conn.execute(
        "SELECT (SELECT count(*) FROM connection_objects o WHERE o.snapshot_id = %(s)s) AS objects, "
        "(SELECT count(*) FROM connection_columns k JOIN connection_objects o ON o.id = k.object_id "
        " WHERE o.snapshot_id = %(s)s) AS columns, "
        "(SELECT count(*) FROM connection_constraints k JOIN connection_objects o ON o.id = k.object_id "
        " WHERE o.snapshot_id = %(s)s) AS constraints, "
        "(SELECT count(*) FROM connection_indexes i JOIN connection_objects o ON o.id = i.object_id "
        " WHERE o.snapshot_id = %(s)s) AS indexes",
        {"s": snapshot_id},
    ).fetchone()
    return row


_HEAD = (
    "INSERT INTO schema_changes (connection_id, from_snapshot_id, to_snapshot_id, to_version, object_type, "
    "schema_name, "
    "object_name, sub_name, change_type, previous, current) "
)
_PAIRS = (
    "pairs AS (SELECT x.id AS xid, y.id AS yid, x.schema_name, x.object_name FROM connection_objects x "
    "JOIN connection_objects y ON y.snapshot_id = %(b)s AND y.schema_name = x.schema_name "
    "AND y.object_name = x.object_name WHERE x.snapshot_id = %(a)s)"
)
_FIXED = "%(c)s, %(a)s, %(b)s, %(v)s"
CHANGE_STATEMENTS = (
    # schemas that appeared or disappeared
    "WITH a AS (SELECT DISTINCT schema_name FROM connection_objects WHERE snapshot_id = %(a)s), "
    "b AS (SELECT DISTINCT schema_name FROM connection_objects WHERE snapshot_id = %(b)s) "
    + _HEAD
    + f"SELECT {_FIXED}, 'schema', coalesce(b.schema_name, a.schema_name), '', '', "
    "CASE WHEN a.schema_name IS NULL THEN 'added' ELSE 'removed' END, NULL::jsonb, NULL::jsonb "
    "FROM a FULL JOIN b ON a.schema_name = b.schema_name WHERE a.schema_name IS NULL OR b.schema_name IS NULL",
    # tables and views that appeared, disappeared, or changed what they are (a table that became a view)
    "WITH a AS (SELECT o.id, o.schema_name, o.object_name, o.object_type FROM connection_objects o "
    "WHERE o.snapshot_id = %(a)s), "
    "b AS (SELECT o.id, o.schema_name, o.object_name, o.object_type FROM connection_objects o "
    "WHERE o.snapshot_id = %(b)s) "
    + _HEAD
    + f"SELECT {_FIXED}, CASE WHEN coalesce(b.object_type, a.object_type) = 'table' THEN 'table' ELSE 'view' END, "
    "coalesce(b.schema_name, a.schema_name), coalesce(b.object_name, a.object_name), '', "
    "CASE WHEN a.id IS NULL THEN 'added' WHEN b.id IS NULL THEN 'removed' ELSE 'modified' END, "
    "CASE WHEN a.id IS NULL THEN NULL ELSE jsonb_build_object('type', a.object_type, 'columns', "
    "(SELECT count(*) FROM connection_columns k WHERE k.object_id = a.id)) END, "
    "CASE WHEN b.id IS NULL THEN NULL ELSE jsonb_build_object('type', b.object_type, 'columns', "
    "(SELECT count(*) FROM connection_columns k WHERE k.object_id = b.id)) END "
    "FROM a FULL JOIN b ON a.schema_name = b.schema_name AND a.object_name = b.object_name "
    "WHERE a.id IS NULL OR b.id IS NULL OR a.object_type <> b.object_type",
    # columns of objects that exist in both snapshots
    "WITH "
    + _PAIRS
    + ", ca AS (SELECT p.schema_name, p.object_name, k.column_name, k.data_type, k.is_nullable FROM pairs p "
    "JOIN connection_columns k ON k.object_id = p.xid), "
    "cb AS (SELECT p.schema_name, p.object_name, k.column_name, k.data_type, k.is_nullable FROM pairs p "
    "JOIN connection_columns k ON k.object_id = p.yid) "
    + _HEAD
    + f"SELECT {_FIXED}, 'column', coalesce(cb.schema_name, ca.schema_name), coalesce(cb.object_name, ca.object_name), "
    "coalesce(cb.column_name, ca.column_name), "
    "CASE WHEN ca.column_name IS NULL THEN 'added' WHEN cb.column_name IS NULL THEN 'removed' ELSE 'modified' END, "
    "CASE WHEN ca.column_name IS NULL THEN NULL ELSE jsonb_build_object('data_type', ca.data_type, "
    "'nullable', ca.is_nullable) END, "
    "CASE WHEN cb.column_name IS NULL THEN NULL ELSE jsonb_build_object('data_type', cb.data_type, "
    "'nullable', cb.is_nullable) END "
    "FROM ca FULL JOIN cb ON ca.schema_name = cb.schema_name AND ca.object_name = cb.object_name "
    "AND ca.column_name = cb.column_name WHERE ca.column_name IS NULL OR cb.column_name IS NULL "
    "OR ca.data_type <> cb.data_type OR ca.is_nullable <> cb.is_nullable",
    # primary, unique and foreign keys of objects that exist in both (a foreign key is reported as a relationship)
    "WITH " + _PAIRS + ", ka AS (SELECT p.schema_name, p.object_name, k.kind, "
    "(CASE WHEN k.kind = 'primary_key' THEN '' ELSE k.constraint_name END) AS ident, k.constraint_name, k.columns, "
    "k.ref_schema, k.ref_object, k.ref_columns FROM pairs p JOIN connection_constraints k ON k.object_id = p.xid), "
    "kb AS (SELECT p.schema_name, p.object_name, k.kind, "
    "(CASE WHEN k.kind = 'primary_key' THEN '' ELSE k.constraint_name END) AS ident, k.constraint_name, k.columns, "
    "k.ref_schema, k.ref_object, k.ref_columns FROM pairs p JOIN connection_constraints k ON k.object_id = p.yid) "
    + _HEAD
    + f"SELECT {_FIXED}, CASE WHEN coalesce(kb.kind, ka.kind) = 'foreign_key' THEN 'relationship' ELSE 'constraint' "
    f"END, "
    "coalesce(kb.schema_name, ka.schema_name), coalesce(kb.object_name, ka.object_name), "
    "coalesce(kb.constraint_name, ka.constraint_name), "
    "CASE WHEN ka.kind IS NULL THEN 'added' WHEN kb.kind IS NULL THEN 'removed' ELSE 'modified' END, "
    "CASE WHEN ka.kind IS NULL THEN NULL ELSE jsonb_build_object('kind', ka.kind, 'columns', to_jsonb(ka.columns), "
    "'ref_schema', ka.ref_schema, 'ref_object', ka.ref_object, 'ref_columns', to_jsonb(ka.ref_columns)) END, "
    "CASE WHEN kb.kind IS NULL THEN NULL ELSE jsonb_build_object('kind', kb.kind, 'columns', to_jsonb(kb.columns), "
    "'ref_schema', kb.ref_schema, 'ref_object', kb.ref_object, 'ref_columns', to_jsonb(kb.ref_columns)) END "
    "FROM ka FULL JOIN kb ON ka.schema_name = kb.schema_name AND ka.object_name = kb.object_name "
    "AND ka.kind = kb.kind AND ka.ident = kb.ident WHERE ka.kind IS NULL OR kb.kind IS NULL "
    "OR ka.columns <> kb.columns OR ka.ref_schema IS DISTINCT FROM kb.ref_schema "
    "OR ka.ref_object IS DISTINCT FROM kb.ref_object OR ka.ref_columns IS DISTINCT FROM kb.ref_columns",
    # indexes of objects that exist in both
    "WITH "
    + _PAIRS
    + ", ia AS (SELECT p.schema_name, p.object_name, (CASE WHEN i.is_primary THEN '' ELSE i.index_name END) AS ident, "
    "i.index_name, i.columns, i.is_unique, i.is_primary FROM pairs p JOIN connection_indexes i ON i.object_id = "
    "p.xid), "
    "ib AS (SELECT p.schema_name, p.object_name, (CASE WHEN i.is_primary THEN '' ELSE i.index_name END) AS ident, "
    "i.index_name, i.columns, i.is_unique, i.is_primary FROM pairs p JOIN connection_indexes i ON i.object_id = p.yid) "
    + _HEAD
    + f"SELECT {_FIXED}, 'index', coalesce(ib.schema_name, ia.schema_name), coalesce(ib.object_name, ia.object_name), "
    "coalesce(ib.index_name, ia.index_name), "
    "CASE WHEN ia.ident IS NULL THEN 'added' WHEN ib.ident IS NULL THEN 'removed' ELSE 'modified' END, "
    "CASE WHEN ia.ident IS NULL THEN NULL ELSE jsonb_build_object('columns', to_jsonb(ia.columns), "
    "'unique', ia.is_unique, 'primary', ia.is_primary) END, "
    "CASE WHEN ib.ident IS NULL THEN NULL ELSE jsonb_build_object('columns', to_jsonb(ib.columns), "
    "'unique', ib.is_unique, 'primary', ib.is_primary) END "
    "FROM ia FULL JOIN ib ON ia.schema_name = ib.schema_name AND ia.object_name = ib.object_name "
    "AND ia.ident = ib.ident WHERE ia.ident IS NULL OR ib.ident IS NULL OR ia.columns <> ib.columns "
    "OR ia.is_unique <> ib.is_unique OR ia.is_primary <> ib.is_primary",
)


def compute_changes(conn, connection_id, previous_id, snapshot_id, version):
    """Record what differs between two snapshots of one connection, with set-based SQL (nothing is held in memory). At
    most CONNECTOR_MAX_CHANGES rows are kept, the object-level ones first; returns (total found, truncated)."""
    params = {"c": connection_id, "a": previous_id, "b": snapshot_id, "v": version}
    for statement in CHANGE_STATEMENTS:
        conn.execute(statement, params)
    total = conn.execute(
        "SELECT count(*) AS n FROM schema_changes WHERE to_snapshot_id = %s", (snapshot_id,)
    ).fetchone()["n"]
    truncated = total > config.CONNECTOR_MAX_CHANGES
    if truncated:
        conn.execute(
            "DELETE FROM schema_changes WHERE id IN (SELECT id FROM schema_changes WHERE to_snapshot_id = %s "
            f"ORDER BY {CHANGE_ORDER}, schema_name, object_name, sub_name, change_type OFFSET %s)",
            (snapshot_id, config.CONNECTOR_MAX_CHANGES),
        )
    return total, truncated


def prune(conn, connection_id):
    """Keep the newest CONNECTOR_SNAPSHOT_RETENTION snapshots of the connection (never the current one); what hangs off
    an older one (its rows, the changes that led to it) goes with it."""
    conn.execute(
        "DELETE FROM schema_snapshots s USING connections c WHERE c.id = %s AND s.connection_id = c.id "
        "AND s.id <> c.current_snapshot_id AND s.version <= (SELECT cs.version FROM schema_snapshots cs "
        "WHERE cs.id = c.current_snapshot_id) - %s",
        (connection_id, config.CONNECTOR_SNAPSHOT_RETENTION),
    )


# ---------- reading ----------

SNAPSHOT_COLUMNS = (
    "s.id, s.version, s.fingerprint, s.fingerprint_version, s.provider, s.database_name, s.created_at, s.verified_at, "
    "s.objects_count, s.columns_count, s.constraints_count, s.indexes_count, s.changes_count, s.changes_truncated, "
    "(s.id = c.current_snapshot_id) AS is_current"
)


def _snapshot(row):
    row = dict(row)
    row["id"] = str(row["id"])
    return row


def list_snapshots(owner, connection_id):
    with db.pool().connection() as conn:
        rows = conn.execute(
            f"SELECT {SNAPSHOT_COLUMNS} FROM schema_snapshots s JOIN connections c ON c.id = s.connection_id "
            "WHERE c.owner_principal_id = %s AND c.id = %s ORDER BY s.version DESC",
            (owner, connection_id),
        ).fetchall()
    return [_snapshot(r) for r in rows]


def get_snapshot(owner, connection_id, snapshot_id):
    """One snapshot (by id) with the number of tables and views in each of its schemas, or None."""
    with db.pool().connection() as conn:
        row = conn.execute(
            f"SELECT {SNAPSHOT_COLUMNS} FROM schema_snapshots s JOIN connections c ON c.id = s.connection_id "
            "WHERE c.owner_principal_id = %s AND c.id = %s AND s.id = %s",
            (owner, connection_id, snapshot_id),
        ).fetchone()
        if row is None:
            return None
        schemas = conn.execute(
            "SELECT o.schema_name, count(*) FILTER (WHERE o.object_type = 'table') AS tables, "
            "count(*) FILTER (WHERE o.object_type <> 'table') AS views FROM connection_objects o "
            "JOIN schema_snapshots s ON s.id = o.snapshot_id JOIN connections c ON c.id = s.connection_id "
            "WHERE c.owner_principal_id = %s AND c.id = %s AND s.id = %s GROUP BY o.schema_name "
            "ORDER BY o.schema_name",
            (owner, connection_id, snapshot_id),
        ).fetchall()
    return {**_snapshot(row), "schemas": [dict(s) for s in schemas]}


def list_changes(
    owner, connection_id, *, version=None, since_version=None, object_type=None, change_type=None, limit=200, offset=0
):
    """(rows, total, counts by change type) of the recorded changes, newest snapshot first."""
    where, params = "c.owner_principal_id = %s AND c.id = %s", [owner, connection_id]
    if version is not None:
        where += " AND x.to_version = %s"
        params.append(version)
    if since_version is not None:
        where += " AND x.to_version > %s"
        params.append(since_version)
    if object_type:
        where += " AND x.object_type = %s"
        params.append(object_type)
    if change_type:
        where += " AND x.change_type = %s"
        params.append(change_type)
    with db.pool().connection() as conn:
        total = conn.execute(
            f"SELECT count(*) AS n FROM schema_changes x JOIN connections c ON c.id = x.connection_id WHERE {where}",
            params,
        ).fetchone()["n"]
        summary = conn.execute(
            "SELECT x.change_type, count(*) AS n FROM schema_changes x JOIN connections c ON c.id = x.connection_id "
            f"WHERE {where} GROUP BY x.change_type",
            params,
        ).fetchall()
        rows = conn.execute(
            "SELECT x.to_version, x.object_type, x.schema_name, x.object_name, x.sub_name, x.change_type, x.previous, "
            "x.current, x.detected_at FROM schema_changes x JOIN connections c ON c.id = x.connection_id "
            f"WHERE {where} ORDER BY x.to_version DESC, {CHANGE_ORDER.replace('object_type', 'x.object_type')}, "
            "x.schema_name, x.object_name, x.sub_name, x.change_type LIMIT %s OFFSET %s",
            (*params, limit, offset),
        ).fetchall()
    return [dict(r) for r in rows], total, {r["change_type"]: r["n"] for r in summary}


def changes_since(owner, connection_id, version):
    """The changes recorded after snapshot `version` (at most 20000), for judging which of them touch the selection."""
    with db.pool().connection() as conn:
        rows = conn.execute(
            "SELECT x.object_type, x.schema_name, x.object_name, x.sub_name, x.change_type FROM schema_changes x "
            "JOIN connections c ON c.id = x.connection_id WHERE c.owner_principal_id = %s AND c.id = %s "
            "AND x.to_version > %s ORDER BY x.to_version, x.schema_name, x.object_name, x.sub_name LIMIT 20000",
            (owner, connection_id, version),
        ).fetchall()
    return [dict(r) for r in rows]


def facts(owner, connection_id):
    """What freshness is computed from: the discovery fields of the connection and its current snapshot."""
    with db.pool().connection() as conn:
        row = conn.execute(
            "SELECT now() AS now, c.discovery_started_at, c.last_discovery_at, c.last_discovery_status, "
            "c.last_discovery_error, c.context_confirmed_version, c.selection_revision, c.schema_discovered_at, "
            "s.id AS snapshot_id, s.version, s.fingerprint, s.created_at, s.verified_at, s.objects_count, "
            "s.columns_count "
            "FROM connections c LEFT JOIN schema_snapshots s ON s.id = c.current_snapshot_id "
            "WHERE c.owner_principal_id = %s AND c.id = %s",
            (owner, connection_id),
        ).fetchone()
    return dict(row) if row else None


# ---------- the selection ----------


def get_selection(owner, connection_id):
    """(revision, items) or None when the connection is not this owner's."""
    with db.pool().connection() as conn:
        found = conn.execute(
            "SELECT c.selection_revision FROM connections c WHERE c.owner_principal_id = %s AND c.id = %s",
            (owner, connection_id),
        ).fetchone()
        if found is None:
            return None
        rows = conn.execute(
            "SELECT s.kind, s.schema_name, s.object_name, s.column_name, s.mode FROM connection_selections s "
            "JOIN connections c ON c.id = s.connection_id WHERE c.owner_principal_id = %s AND c.id = %s "
            "ORDER BY s.schema_name, s.object_name, s.column_name, s.kind",
            (owner, connection_id),
        ).fetchall()
    items = [
        {
            "kind": r["kind"],
            "schema": r["schema_name"],
            "object": r["object_name"],
            "column": r["column_name"],
            "mode": r["mode"],
        }
        for r in rows
    ]
    return found["selection_revision"], items


def existing(owner, connection_id, items):
    """Which of `items` (a list of selection items) name something that exists in the CURRENT snapshot: a set of
    (kind, schema, object, column)."""
    found = set()
    with db.pool().connection() as conn:
        for kind in ("schema", "table", "column"):
            chosen = [i for i in items if i["kind"] == kind]
            for start in range(0, len(chosen), 5000):
                part = chosen[start : start + 5000]
                schemas, objects, columns = (
                    [i["schema"] for i in part],
                    [i["object"] for i in part],
                    [i["column"] for i in part],
                )
                if kind == "schema":
                    sql = (
                        "SELECT DISTINCT t.s AS schema_name, '' AS object_name, '' AS column_name FROM "
                        "unnest(%s::text[]) AS t(s) "
                        "JOIN connection_objects o ON o.schema_name = t.s JOIN connections c ON c.id = o.connection_id "
                        "AND o.snapshot_id = c.current_snapshot_id WHERE c.owner_principal_id = %s AND c.id = %s"
                    )
                    args = (schemas, owner, connection_id)
                elif kind == "table":
                    sql = (
                        "SELECT t.s AS schema_name, t.o AS object_name, '' AS column_name FROM unnest(%s::text[], "
                        "%s::text[]) AS t(s, o) "
                        "JOIN connection_objects o ON o.schema_name = t.s AND o.object_name = t.o "
                        "JOIN connections c ON c.id = o.connection_id AND o.snapshot_id = c.current_snapshot_id "
                        "WHERE c.owner_principal_id = %s AND c.id = %s"
                    )
                    args = (schemas, objects, owner, connection_id)
                else:
                    sql = (
                        "SELECT t.s AS schema_name, t.o AS object_name, t.c AS column_name FROM "
                        "unnest(%s::text[], %s::text[], %s::text[]) AS t(s, o, c) "
                        "JOIN connection_objects o ON o.schema_name = t.s AND o.object_name = t.o "
                        "JOIN connections c ON c.id = o.connection_id AND o.snapshot_id = c.current_snapshot_id "
                        "JOIN connection_columns k ON k.object_id = o.id AND k.column_name = t.c "
                        "WHERE c.owner_principal_id = %s AND c.id = %s"
                    )
                    args = (schemas, objects, columns, owner, connection_id)
                for r in conn.execute(sql, args):
                    found.add((kind, r["schema_name"], r["object_name"], r["column_name"]))
    return found


def put_selection(owner, connection_id, items, base_revision, validate):
    """Replace the whole selection in one transaction. `validate(current_items)` is called inside it, with the rows
    stored now, and may raise. With `base_revision` the replacement is refused (selection_conflict) if the selection
    changed since the caller read it. The selection is then confirmed against the current snapshot. Returns the new
    revision."""
    conn = db.standalone_connection()
    try:
        with conn.transaction(), bounded_lock_wait(conn):
            locked = conn.execute(
                "SELECT c.selection_revision, s.version FROM connections c LEFT JOIN schema_snapshots s "
                "ON s.id = c.current_snapshot_id WHERE c.owner_principal_id = %s AND c.id = %s FOR UPDATE OF c",
                (owner, connection_id),
            ).fetchone()
            if locked is None:
                raise ConnectorError("not_found", 404)
            if base_revision is not None and base_revision != locked["selection_revision"]:
                raise ConnectorError("selection_conflict", 409)
            stored = conn.execute(
                "SELECT s.kind, s.schema_name, s.object_name, s.column_name, s.mode FROM connection_selections s "
                "JOIN connections c ON c.id = s.connection_id WHERE c.owner_principal_id = %s AND c.id = %s",
                (owner, connection_id),
            ).fetchall()
            validate(
                [
                    {
                        "kind": r["kind"],
                        "schema": r["schema_name"],
                        "object": r["object_name"],
                        "column": r["column_name"],
                        "mode": r["mode"],
                    }
                    for r in stored
                ]
            )
            conn.execute(
                "DELETE FROM connection_selections s USING connections c WHERE s.connection_id = c.id "
                "AND c.owner_principal_id = %s AND c.id = %s",
                (owner, connection_id),
            )
            with conn.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO connection_selections (connection_id, kind, schema_name, object_name, column_name, "
                    "mode) "
                    "SELECT c.id, %s, %s, %s, %s, %s FROM connections c WHERE c.owner_principal_id = %s AND c.id = %s",
                    [
                        (i["kind"], i["schema"], i["object"], i["column"], i["mode"], owner, connection_id)
                        for i in items
                    ],
                )
            revision = locked["selection_revision"] + 1
            conn.execute(
                "UPDATE connections c SET selection_revision = %s, context_confirmed_version = coalesce(%s, 0) "
                "WHERE c.owner_principal_id = %s AND c.id = %s",
                (revision, locked["version"], owner, connection_id),
            )
    finally:
        conn.close()
    return revision


def acknowledge(owner, connection_id, version):
    """The owner has looked at the changes up to snapshot `version`: the selection is confirmed against it. Only the
    CURRENT version can be confirmed (a newer snapshot may have come in since the page was drawn)."""
    with db.pool().connection() as conn:
        with conn.transaction(), bounded_lock_wait(conn):
            row = conn.execute(
                "SELECT s.version FROM connections c LEFT JOIN schema_snapshots s ON s.id = c.current_snapshot_id "
                "WHERE c.owner_principal_id = %s AND c.id = %s FOR UPDATE OF c",
                (owner, connection_id),
            ).fetchone()
            if row is None:
                raise ConnectorError("not_found", 404)
            if row["version"] is None or row["version"] != version:
                raise ConnectorError("snapshot_changed", 409)
            conn.execute(
                "UPDATE connections c SET context_confirmed_version = %s WHERE c.owner_principal_id = %s AND c.id = %s",
                (version, owner, connection_id),
            )


# ---------- what the context is made from (the current snapshot only) ----------


class SnapshotSource:
    """The current snapshot of one connection of one owner, as the context builder reads it."""

    def __init__(self, owner, connection_id, connection, snapshot, descriptions):
        self._owner, self._id = owner, connection_id
        self.provider, self.database_name = connection["provider"], connection["database_name"]
        self.snapshot, self.descriptions = snapshot, descriptions

    def objects(self):
        with db.pool().connection() as conn:
            rows = conn.execute(
                "SELECT o.schema_name, o.object_name, o.object_type FROM connection_objects o "
                "JOIN connections c ON c.id = o.connection_id AND o.snapshot_id = c.current_snapshot_id "
                "WHERE c.owner_principal_id = %s AND c.id = %s",
                (self._owner, self._id),
            ).fetchall()
        return [(r["schema_name"], r["object_name"], r["object_type"]) for r in rows]

    def constraints(self, selected):
        """The keys of the selected objects, in one query (a few rows per object)."""
        out = {}
        with db.pool().connection() as conn:
            cursor = conn.execute(
                "SELECT o.schema_name, o.object_name, k.kind, k.constraint_name, k.columns, k.ref_schema, "
                "k.ref_object, "
                "k.ref_columns FROM connection_constraints k JOIN connection_objects o ON o.id = k.object_id "
                "JOIN connections c ON c.id = o.connection_id AND o.snapshot_id = c.current_snapshot_id "
                "WHERE c.owner_principal_id = %s AND c.id = %s",
                (self._owner, self._id),
            )
            for r in cursor:
                key = (r["schema_name"], r["object_name"])
                if key in selected:
                    out.setdefault(key, []).append(
                        {
                            "kind": r["kind"],
                            "name": r["constraint_name"],
                            "columns": list(r["columns"]),
                            "ref_schema": r["ref_schema"],
                            "ref_object": r["ref_object"],
                            "ref_columns": list(r["ref_columns"] or []),
                        }
                    )
        return out

    def columns(self, batch):
        schemas, names = [k[0] for k in batch], [k[1] for k in batch]
        out = {}
        with db.pool().connection() as conn:
            rows = conn.execute(
                "SELECT o.schema_name, o.object_name, k.column_name, k.data_type, k.is_nullable FROM "
                "unnest(%s::text[], %s::text[]) AS t(s, n) JOIN connection_objects o ON o.schema_name = t.s "
                "AND o.object_name = t.n JOIN connections c ON c.id = o.connection_id "
                "AND o.snapshot_id = c.current_snapshot_id JOIN connection_columns k ON k.object_id = o.id "
                "WHERE c.owner_principal_id = %s AND c.id = %s ORDER BY o.schema_name, o.object_name, k.ordinal",
                (schemas, names, self._owner, self._id),
            ).fetchall()
        for r in rows:
            out.setdefault((r["schema_name"], r["object_name"]), []).append(
                (r["column_name"], r["data_type"], r["is_nullable"])
            )
        return out
