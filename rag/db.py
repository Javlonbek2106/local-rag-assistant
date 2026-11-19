"""PostgreSQL: the source of truth for documents, their versions and the indexing job queue.

A document has many versions (document_versions) and at most one ACTIVE one (documents.active_version_id): the
only one that searches may see. A version row is its own job. Workers claim QUEUED versions with
FOR UPDATE SKIP LOCKED, keep a heartbeat while they work, and finish with activate() or fail(). Every finishing
call checks `claimed_by`, so a worker that was declared dead and replaced can never overwrite the result of the
worker that took over. Qdrant only holds a derived, rebuildable index.

`documents.state / chunks / error / size_bytes` are a summary of the versions, rewritten by _sync_document in the
same transaction as every change of a version's status, so the two levels cannot disagree.

Lock order everywhere: the version being changed, then its document, then the version it replaces.
"""
import functools
import logging
import threading
import uuid
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from rag import config, dbroles, storage

log = logging.getLogger(__name__)
MIGRATIONS = Path(__file__).parent / "migrations"
MIGRATION_LOCK = 727001  # arbitrary key for pg_advisory_lock: api and worker may start at the same moment
INFLIGHT = ("PROCESSING", "EMBEDDING", "INDEXING", "VERIFYING")  # a worker holds the version
_INFLIGHT = "('PROCESSING', 'EMBEDDING', 'INDEXING', 'VERIFYING')"
# A worker's lease: its last heartbeat is younger than JOB_STALE_AFTER_S (the same limit after which requeue_stale takes
# the version back). Expiry is final: every call a worker makes on its version checks the lease, and none of them
# renews an expired one, so a worker that stalled past the limit can never finish, even before anyone takes over.
_LEASE = "heartbeat_at > now() - make_interval(secs => %(lease)s)"
# Progress is only meaningful while a worker holds the version: every statement that releases it or moves it to a new
# stage clears it, so a stale position is never shown for a job that is queued, failed or finished.
_NO_PROGRESS = "progress_unit = NULL, progress_done = NULL, progress_total = NULL, progress_at = NULL"
DOCUMENT_SELECT = f"""
    SELECT d.id, d.name, d.size_bytes, d.state, d.chunks, d.activation_generation,
           CASE WHEN d.state = 'failed' THEN d.error END AS error,
           CASE WHEN d.state = 'done' THEN d.error END AS update_error,
           d.created_at, d.updated_at, av.version_number AS version,
           (SELECT max(x.version_number) FROM document_versions x WHERE x.document_id = d.id) AS latest_version,
           extract(epoch FROM now() - w.started_at)::int AS seconds,
           w.status AS stage, w.progress_unit, w.progress_done, w.progress_total
    FROM documents d LEFT JOIN document_versions av ON av.id = d.active_version_id
    LEFT JOIN LATERAL (SELECT x.status, x.started_at, x.progress_unit, x.progress_done, x.progress_total
                       FROM document_versions x WHERE x.document_id = d.id AND x.status IN {_INFLIGHT}
                       ORDER BY x.version_number DESC LIMIT 1) w ON true
"""
VERSION_COLUMNS = f"""
    v.id, v.document_id, d.name, v.version_number, v.file_name, v.file_hash, v.file_size, v.file_path, v.status,
    v.chunk_count, v.error_message, v.attempts, v.claimed_by, v.created_at, v.indexed_at, v.activated_at,
    v.uploaded_generation, d.activation_generation, v.progress_unit, v.progress_done, v.progress_total,
    (d.active_version_id = v.id) AS active,
    CASE WHEN v.status IN {_INFLIGHT} THEN extract(epoch FROM now() - v.started_at)::int END AS seconds
"""


class Busy(Exception):
    """The document is being indexed right now, so it cannot be deleted."""


class VersionConflict(Exception):
    """The requested change of the active version is not allowed in the version's current state."""


class _RuntimeConnection(psycopg.Connection):
    """A connection of the hardened runtime pool: the password is read from its secret file each time a connection is
    made, never held in the pool's settings, so a rotated password is used by the next connection with no restart."""

    @classmethod
    def connect(cls, conninfo="", **kwargs):
        return super().connect(conninfo, password=dbroles.read_password(dbroles.APP), **kwargs)


def _open_pool():
    kwargs = {"row_factory": dict_row, "autocommit": True}
    if config.DB_SCHEMA:
        kwargs["options"] = f"-c search_path={config.DB_SCHEMA}"
    if config.DB_HARDENED:  # the application's own role, rag_app; a missing or unsafe secret stops here, clearly
        dbroles.read_password(dbroles.APP)
        return ConnectionPool(dbroles.conninfo(dbroles.APP), connection_class=_RuntimeConnection, min_size=1,
                              max_size=config.DB_POOL_SIZE, kwargs=kwargs, open=True)
    return ConnectionPool(config.DATABASE_URL, min_size=1, max_size=config.DB_POOL_SIZE, kwargs=kwargs, open=True)


_pool_lock = threading.Lock()
_cached_pool = functools.lru_cache(maxsize=1)(_open_pool)


def pool():
    """The one connection pool. `lru_cache` alone does not lock: two threads that arrive while it is still empty would
    each open a pool and one would leak (with its connections and threads). The lock makes the first caller open it and
    the others wait for it."""
    with _pool_lock:
        return _cached_pool()


pool.cache_info = _cached_pool.cache_info  # the callers (and the tests) that reset or inspect the cache
pool.cache_clear = _cached_pool.cache_clear


def close():
    if pool.cache_info().currsize:  # never open a pool only to close it (the hardened one needs a secret to open)
        pool().close()
    pool.cache_clear()


def runtime_url():
    """The URL of the database as the application sees it (for the backup tools); in hardened mode it includes the
    password of rag_app, in memory only."""
    return dbroles.url_with_password(dbroles.APP) if config.DB_HARDENED else config.DATABASE_URL


def standalone_connection():
    """A connection of the runtime role that is not part of the pool, for work that keeps a transaction open for a long
    time (the connector's schema discovery): it must not hold one of the few pooled connections the rest of the
    application needs. The caller closes it."""
    kwargs = {"row_factory": dict_row, "autocommit": True, "connect_timeout": 10}
    if config.DB_SCHEMA:
        kwargs["options"] = f"-c search_path={config.DB_SCHEMA}"
    if config.DB_HARDENED:
        return _RuntimeConnection.connect(dbroles.conninfo(dbroles.APP), **kwargs)
    return psycopg.connect(config.DATABASE_URL, **kwargs)


def _migrator_connection():
    """A short-lived connection as rag_migrator: the only role that may change the schema. Hardened mode only."""
    kwargs = {"row_factory": dict_row, "autocommit": True, "connect_timeout": 10,
              "password": dbroles.read_password(dbroles.MIGRATOR)}
    if config.DB_SCHEMA:
        kwargs["options"] = f"-c search_path={config.DB_SCHEMA}"
    return psycopg.connect(dbroles.conninfo(dbroles.MIGRATOR), **kwargs)


def _apply_pending(conn):
    """Apply every migrations/*.sql not yet applied, serialised by an advisory lock, on `conn`."""
    conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK,))
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                     "(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        done = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.stem in done:
                continue
            with conn.transaction():
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.stem,))
            log.info("applied migration %s", path.stem)
        if config.DB_HARDENED:  # still under the lock: nobody reads a half-granted state as "current"
            dbroles.apply_runtime_grants(conn, config.DB_SCHEMA or "public")
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK,))


def _is_current():
    """Hardened mode: asked by the runtime role itself. True when every migration is recorded and rag_app already has
    exactly its permissions; any doubt (no table yet, no right to read it, no connection) is False."""
    try:
        with pool().connection(timeout=config.SECURITY_DB_TIMEOUT_S) as conn:
            done = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}
            if {path.stem for path in MIGRATIONS.glob("*.sql")} - done:
                return False
            return dbroles.grants_in_place(conn, config.DB_SCHEMA or "public")
    except psycopg.Error:
        return False


def migrate():
    """Apply every migrations/*.sql that has not been applied yet. Safe to call from several processes.

    Single-role mode (default): the one role does it, as before. Hardened mode: the runtime role rag_app only LOOKS; if
    something is pending, rag_migrator (the only role with DDL rights) connects, takes the advisory lock, applies it,
    refreshes rag_app's permissions and disconnects. The API, the worker and the CLI may all call this at once: the lock
    makes the first one do the work and the others find it done. If the migrator's secret is missing the call fails; it
    never falls back to another role.
    """
    if not config.DB_HARDENED:
        with pool().connection() as conn:
            _apply_pending(conn)
        return
    if _is_current():
        return
    with _migrator_connection() as conn:
        _apply_pending(conn)


# ---------- documents and versions ----------

def _sync_document(conn, doc_id):
    """Rewrite the document's summary from its versions. Called inside the transaction that changed a version."""
    conn.execute(
        f"""
        UPDATE documents d SET
            state = CASE
                WHEN EXISTS (SELECT 1 FROM document_versions v
                             WHERE v.document_id = d.id AND v.status IN {_INFLIGHT}) THEN 'processing'
                WHEN EXISTS (SELECT 1 FROM document_versions v
                             WHERE v.document_id = d.id AND v.status = 'QUEUED') THEN 'queued'
                WHEN EXISTS (SELECT 1 FROM document_versions v
                             WHERE v.document_id = d.id AND v.status = 'UPLOADED') THEN 'uploading'
                WHEN d.active_version_id IS NOT NULL THEN 'done'
                ELSE 'failed' END,
            chunks = (SELECT v.chunk_count FROM document_versions v WHERE v.id = d.active_version_id),
            error = (SELECT CASE WHEN v.status = 'FAILED' THEN v.error_message END FROM document_versions v
                     WHERE v.document_id = d.id ORDER BY v.version_number DESC LIMIT 1),
            size_bytes = COALESCE((SELECT v.file_size FROM document_versions v WHERE v.document_id = d.id
                                   ORDER BY v.version_number DESC LIMIT 1), d.size_bytes),
            updated_at = now()
        WHERE d.id = %s
        """,
        (doc_id,),
    )


def create_version(name, size_bytes, file_hash):
    """Register an upload as the next version of the document `name` (creating the document if it is new).

    Returns {"id", "document_id", "version_number", "file_path", "created"}. The version starts as UPLOADED: the caller
    saves the file at `file_path` (rag.storage) and then calls enqueue(). Uploads of the same name are serialised by
    the document row, so version numbers never collide, and the upload is stamped with the document's rollback count
    (uploaded_generation): a rollback committed after this point makes it a stale candidate. A file identical to the
    newest version that did not fail creates nothing (created=False) and returns that version, unless that version
    is a stale candidate (uploaded before the latest rollback and not the active version): it will never become
    active on its own, so uploading the same file again after a rollback is a new decision and gets a new version.
    """
    with pool().connection() as conn, conn.transaction():
        doc = conn.execute(
            "INSERT INTO documents (name, size_bytes, state) VALUES (%s, %s, 'uploading') "
            "ON CONFLICT (name) DO UPDATE SET updated_at = now() "
            "RETURNING id, activation_generation",  # the update also locks the row
            (name, size_bytes),
        ).fetchone()
        newest = conn.execute(
            "SELECT v.id, v.version_number, v.file_hash, v.file_path, v.uploaded_generation, "
            "(d.active_version_id = v.id) AS active FROM document_versions v JOIN documents d ON d.id = v.document_id "
            "WHERE v.document_id = %s AND v.status <> 'FAILED' ORDER BY v.version_number DESC LIMIT 1", (doc["id"],),
        ).fetchone()
        if (newest and newest["file_hash"] == file_hash
                and (newest["active"] or newest["uploaded_generation"] == doc["activation_generation"])):
            return {"id": newest["id"], "document_id": doc["id"], "version_number": newest["version_number"],
                    "file_path": newest["file_path"], "created": False}
        number = conn.execute(
            "SELECT coalesce(max(version_number), 0) + 1 AS n FROM document_versions WHERE document_id = %s",
            (doc["id"],),
        ).fetchone()["n"]
        version_id = uuid.uuid4()
        path = storage.relative_path(doc["id"], version_id, name)
        conn.execute(
            "INSERT INTO document_versions (id, document_id, version_number, file_name, file_hash, file_size, "
            "file_path, status, uploaded_generation) VALUES (%s, %s, %s, %s, %s, %s, %s, 'UPLOADED', %s)",
            (version_id, doc["id"], number, name, file_hash, size_bytes, path, doc["activation_generation"]),
        )
        _sync_document(conn, doc["id"])
    return {"id": version_id, "document_id": doc["id"], "version_number": number, "file_path": path,
            "created": True}


def enqueue(version_id):
    """The file is saved: let workers take it. False if the version is no longer UPLOADED."""
    with pool().connection() as conn, conn.transaction():
        row = conn.execute(
            "UPDATE document_versions SET status = 'QUEUED', updated_at = now() "
            "WHERE id = %s AND status = 'UPLOADED' RETURNING document_id", (version_id,),
        ).fetchone()
        if row:
            _sync_document(conn, row["document_id"])
    return row is not None


def get_document(name):
    with pool().connection() as conn:
        return conn.execute(f"{DOCUMENT_SELECT} WHERE d.name = %s", (name,)).fetchone()


def list_documents():
    with pool().connection() as conn:
        return conn.execute(f"{DOCUMENT_SELECT} ORDER BY d.name").fetchall()


def queue_length():
    with pool().connection() as conn:
        sql = "SELECT count(*) AS n FROM documents WHERE state IN ('queued', 'processing')"
        return conn.execute(sql).fetchone()["n"]


def ping():
    """PostgreSQL answers within a couple of seconds (the readiness check)."""
    with pool().connection(timeout=2) as conn:
        conn.execute("SELECT 1")


def status_snapshot():
    """The numbers behind /api/status, from the existing tables with read-only queries and a bounded result size
    (at most 20 versions in flight and 10 recent errors, however many documents there are)."""
    with pool().connection() as conn:
        documents = conn.execute(
            "SELECT count(*) AS total, count(*) FILTER (WHERE active_version_id IS NOT NULL) AS searchable "
            "FROM documents").fetchone()
        by_state = {r["state"]: r["n"] for r in conn.execute(
            "SELECT state, count(*) AS n FROM documents GROUP BY state")}
        by_status = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, count(*) AS n FROM document_versions GROUP BY status")}
        queue = conn.execute(
            "SELECT count(*) AS queued, extract(epoch FROM now() - min(updated_at))::int AS oldest "
            "FROM document_versions WHERE status = 'QUEUED'").fetchone()
        in_flight = conn.execute(
            f"SELECT d.name AS document, v.version_number AS version, v.status AS stage, v.attempts AS attempt, "
            f"extract(epoch FROM now() - v.started_at)::int AS running_seconds, "
            f"extract(epoch FROM now() - v.heartbeat_at)::int AS heartbeat_age_seconds, "
            f"v.progress_unit, v.progress_done, v.progress_total, "
            f"extract(epoch FROM now() - v.progress_at)::int AS progress_age_seconds "
            f"FROM document_versions v JOIN documents d ON d.id = v.document_id "
            f"WHERE v.status IN {_INFLIGHT} ORDER BY v.started_at LIMIT 20").fetchall()
        errors = conn.execute(
            "SELECT d.name AS document, v.version_number AS version, v.status, v.error_message AS error, "
            "v.updated_at AS at FROM document_versions v JOIN documents d ON d.id = v.document_id "
            "WHERE v.error_message IS NOT NULL AND v.status IN ('FAILED', 'QUEUED') "
            "ORDER BY v.updated_at DESC LIMIT 10").fetchall()
    return {"documents": {"total": documents["total"], "searchable": documents["searchable"], "by_state": by_state},
            "versions": {"total": sum(by_status.values()), "by_status": by_status},
            "in_flight_statuses": INFLIGHT, "queued": queue["queued"], "oldest_queued_age_seconds": queue["oldest"],
            "in_flight": in_flight, "last_errors": errors}


def delete_document(name):
    """Delete the document with all its versions and return its id; None if there is no such document.
    Raises Busy while any version is being indexed."""
    with pool().connection() as conn, conn.transaction():
        found = conn.execute("SELECT id FROM documents WHERE name = %s", (name,)).fetchone()
        if found is None:
            return None
        conn.execute("SELECT id FROM document_versions WHERE document_id = %s ORDER BY id FOR UPDATE", (found["id"],))
        conn.execute("SELECT id FROM documents WHERE id = %s FOR UPDATE", (found["id"],))
        busy = conn.execute(
            f"SELECT 1 FROM document_versions WHERE document_id = %s AND status IN {_INFLIGHT}", (found["id"],),
        ).fetchone()
        if busy:
            raise Busy(name)
        conn.execute("DELETE FROM documents WHERE id = %s", (found["id"],))  # the versions go with it
    return found["id"]


def list_versions(name):
    """Every version of the document, newest first; None if there is no such document."""
    with pool().connection() as conn:
        if conn.execute("SELECT 1 FROM documents WHERE name = %s", (name,)).fetchone() is None:
            return None
        return conn.execute(
            f"SELECT {VERSION_COLUMNS} FROM document_versions v JOIN documents d ON d.id = v.document_id "
            "WHERE d.name = %s ORDER BY v.version_number DESC", (name,),
        ).fetchall()


def get_version(name, version_number):
    with pool().connection() as conn:
        return conn.execute(
            f"SELECT {VERSION_COLUMNS} FROM document_versions v JOIN documents d ON d.id = v.document_id "
            "WHERE d.name = %s AND v.version_number = %s", (name, version_number),
        ).fetchone()


def get_version_by_id(version_id):
    with pool().connection() as conn:
        return conn.execute(
            f"SELECT {VERSION_COLUMNS} FROM document_versions v JOIN documents d ON d.id = v.document_id "
            "WHERE v.id = %s", (version_id,),
        ).fetchone()


def active_version_ids():
    """The versions searches may see, as strings. Asked once per question, never per generated token."""
    with pool().connection() as conn:
        rows = conn.execute("SELECT active_version_id::text AS id FROM documents "
                            "WHERE active_version_id IS NOT NULL").fetchall()
    return [row["id"] for row in rows]


def versions_without_hash():
    """Versions registered by the migration, whose file hash is not known yet."""
    with pool().connection() as conn:
        return conn.execute("SELECT id, document_id, file_path FROM document_versions "
                            "WHERE file_hash IS NULL ORDER BY created_at").fetchall()


def set_file_hash(version_id, file_hash):
    with pool().connection() as conn:
        conn.execute("UPDATE document_versions SET file_hash = %s WHERE id = %s AND file_hash IS NULL",
                     (file_hash, version_id))


# ---------- job queue (a version is the job) ----------

def claim_next(worker_id):
    """Take the oldest queued version for this worker, or None. Two workers never get the same one, and a document
    has at most one version in flight: its next version waits until the earlier one is done."""
    with pool().connection() as conn, conn.transaction():
        row = conn.execute(
            f"""
            WITH claimed AS (
                UPDATE document_versions SET status = 'PROCESSING', claimed_by = %s, attempts = attempts + 1,
                       chunk_count = NULL, started_at = now(), heartbeat_at = now(), updated_at = now(),
                       {_NO_PROGRESS}
                WHERE id = (SELECT c.id FROM document_versions c
                            WHERE c.status = 'QUEUED'
                              AND NOT EXISTS (SELECT 1 FROM document_versions o
                                              WHERE o.document_id = c.document_id AND o.id <> c.id
                                                AND (o.status IN {_INFLIGHT}
                                                     OR (o.status = 'QUEUED' AND o.version_number < c.version_number)))
                            ORDER BY c.created_at FOR UPDATE SKIP LOCKED LIMIT 1)
                RETURNING *)
            SELECT {VERSION_COLUMNS} FROM claimed v JOIN documents d ON d.id = v.document_id
            """,
            (worker_id,),
        ).fetchone()
        if row:
            _sync_document(conn, row["document_id"])
    return row


def heartbeat(version_id, worker_id):
    """Say "still alive". False means the claim was lost or its lease expired (this worker was declared dead): an
    expired lease is not renewed."""
    with pool().connection() as conn:
        return conn.execute(
            f"UPDATE document_versions SET heartbeat_at = now() WHERE id = %(id)s AND claimed_by = %(worker)s "
            f"AND status IN {_INFLIGHT} AND {_LEASE} RETURNING id",
            {"id": version_id, "worker": worker_id, "lease": config.JOB_STALE_AFTER_S},
        ).fetchone() is not None


def set_stage(version_id, worker_id, status):
    """Move a held version to the next stage (PROCESSING, EMBEDDING, INDEXING, VERIFYING). False if the claim was lost
    or the lease expired. The document stays 'processing' through all of them, so its summary does not change. A new
    stage starts without a position: the worker reports it again with set_progress()."""
    if status not in INFLIGHT:
        raise ValueError(status)
    with pool().connection() as conn:
        return conn.execute(
            f"UPDATE document_versions SET status = %(status)s, heartbeat_at = now(), updated_at = now(), "
            f"{_NO_PROGRESS} "
            f"WHERE id = %(id)s AND claimed_by = %(worker)s AND status IN {_INFLIGHT} AND {_LEASE} RETURNING id",
            {"status": status, "id": version_id, "worker": worker_id, "lease": config.JOB_STALE_AFTER_S},
        ).fetchone() is not None


def set_progress(version_id, worker_id, unit, done, total=None):
    """Record where a held version is, e.g. ("pages", 120, 381). False if the claim was lost or the lease expired:
    like every other call of a worker, an expired worker writes nothing. It does not renew the lease (only heartbeat()
    and set_stage() do), so reporting progress can never keep a worker alive that has stopped heartbeating."""
    with pool().connection() as conn:
        return conn.execute(
            f"UPDATE document_versions SET progress_unit = %(unit)s, progress_done = %(done)s, "
            f"progress_total = %(total)s, progress_at = now() "
            f"WHERE id = %(id)s AND claimed_by = %(worker)s AND status IN {_INFLIGHT} AND {_LEASE} RETURNING id",
            {"unit": unit, "done": done, "total": total, "id": version_id, "worker": worker_id,
             "lease": config.JOB_STALE_AFTER_S},
        ).fetchone() is not None


def record_verified(version_id, worker_id, chunk_count):
    """Record that the version's index was checked and found complete (rag.versioning.verify passed): the chunk count
    is written only now, and activate() accepts nothing without it. False if the claim was lost or the lease expired."""
    with pool().connection() as conn:
        return conn.execute(
            f"UPDATE document_versions SET chunk_count = %(count)s, updated_at = now() "
            f"WHERE id = %(id)s AND claimed_by = %(worker)s AND status = 'VERIFYING' AND {_LEASE} RETURNING id",
            {"count": chunk_count, "id": version_id, "worker": worker_id, "lease": config.JOB_STALE_AFTER_S},
        ).fetchone() is not None


def activate(version_id, worker_id, chunk_count):
    """Make a verified version the active one, in one transaction. Returns "activated", "superseded" (the version is
    only kept, for a later rollback or roll-forward) or None (the claim was lost or the version was not verified;
    nothing changed).

    Steps: lock this version, then its document, then the active version; confirm this worker still holds the claim
    with an unexpired lease, the version is VERIFYING and its verified chunk count was recorded (record_verified) and
    matches; then decide:

    - a rollback happened after this version was uploaded (uploaded_generation < the document's activation_generation):
      the user chose a version since, and this late candidate must not replace that choice -> "superseded";
    - a newer version is already active -> "superseded";
    - otherwise demote the old active version, promote this one and refresh the document summary -> "activated".

    The generation is compared under the document lock, which a rollback also takes, so a rollback either commits
    before this check (and wins) or after this activation (and then overrides it, as the later decision).
    """
    with pool().connection() as conn, conn.transaction():
        mine = conn.execute(
            "SELECT id, document_id, version_number, status, claimed_by, chunk_count, uploaded_generation, "
            "heartbeat_at > now() - make_interval(secs => %s) AS lease_valid "
            "FROM document_versions WHERE id = %s FOR UPDATE", (config.JOB_STALE_AFTER_S, version_id),
        ).fetchone()
        if (mine is None or mine["claimed_by"] != worker_id or mine["status"] != "VERIFYING"
                or not mine["lease_valid"]  # a worker past its lease cannot finish, even if nobody took over yet
                or mine["chunk_count"] != chunk_count):  # chunk_count is NULL until record_verified
            return None
        doc = conn.execute("SELECT active_version_id, activation_generation FROM documents WHERE id = %s FOR UPDATE",
                           (mine["document_id"],)).fetchone()
        current = None
        if doc["active_version_id"]:
            current = conn.execute("SELECT id, version_number FROM document_versions WHERE id = %s FOR UPDATE",
                                   (doc["active_version_id"],)).fetchone()
        rolled_back_since = mine["uploaded_generation"] < doc["activation_generation"]
        released = (f"claimed_by = NULL, heartbeat_at = NULL, started_at = NULL, error_message = NULL, "
                    f"updated_at = now(), {_NO_PROGRESS}")
        if rolled_back_since or (current and current["version_number"] > mine["version_number"]):
            conn.execute(f"UPDATE document_versions SET status = 'SUPERSEDED', chunk_count = %s, indexed_at = now(), "
                         f"{released} WHERE id = %s", (chunk_count, version_id))
            outcome = "superseded"
        else:
            if current:
                conn.execute("UPDATE document_versions SET status = 'SUPERSEDED', updated_at = now() WHERE id = %s",
                             (current["id"],))
            conn.execute(f"UPDATE document_versions SET status = 'READY', chunk_count = %s, indexed_at = now(), "
                         f"activated_at = now(), {released} WHERE id = %s", (chunk_count, version_id))
            conn.execute("UPDATE documents SET active_version_id = %s WHERE id = %s", (version_id, mine["document_id"]))
            outcome = "activated"
        _sync_document(conn, mine["document_id"])
    return outcome


def switch_active(version_id):
    """Roll back to a version that was active once (status SUPERSEDED). Raises VersionConflict otherwise, changing
    nothing. The caller has already checked that its vectors are complete.

    In one transaction (locks: the version, the document, the version being replaced): re-check the status, demote the
    active version, promote this one and add one to the document's activation_generation. That increment is what makes
    the rollback a durable decision: every version uploaded before it and still unfinished is now a stale candidate that
    activate() will only keep, never make active. Versions uploaded afterwards are stamped with the new generation.
    Returns the new activation generation.
    """
    with pool().connection() as conn, conn.transaction():
        mine = conn.execute(
            "SELECT id, document_id, status, chunk_count FROM document_versions WHERE id = %s FOR UPDATE",
            (version_id,),
        ).fetchone()
        if mine is None:
            raise VersionConflict("Versiya topilmadi")
        if mine["status"] != "SUPERSEDED" or mine["chunk_count"] is None:
            raise VersionConflict(f"Bu versiyaga qaytib bo'lmaydi (holati: {mine['status']})")
        doc = conn.execute("SELECT active_version_id FROM documents WHERE id = %s FOR UPDATE",
                           (mine["document_id"],)).fetchone()
        if doc["active_version_id"]:
            conn.execute("SELECT id FROM document_versions WHERE id = %s FOR UPDATE", (doc["active_version_id"],))
            conn.execute("UPDATE document_versions SET status = 'SUPERSEDED', updated_at = now() WHERE id = %s",
                         (doc["active_version_id"],))
        conn.execute("UPDATE document_versions SET status = 'READY', activated_at = now(), updated_at = now() "
                     "WHERE id = %s", (version_id,))
        generation = conn.execute(
            "UPDATE documents SET active_version_id = %s, activation_generation = activation_generation + 1 "
            "WHERE id = %s RETURNING activation_generation", (version_id, mine["document_id"]),
        ).fetchone()["activation_generation"]
        _sync_document(conn, mine["document_id"])
    return generation


def fail(version_id, worker_id, error, retry=True):
    """Record a failure. The active version is not touched. Returns the new state of the version ('queued' to retry,
    'failed' when out of attempts), or None if the claim was lost."""
    with pool().connection() as conn, conn.transaction():
        row = conn.execute(
            f"""
            UPDATE document_versions SET
                status = CASE WHEN %(retry)s AND attempts < %(max)s THEN 'QUEUED' ELSE 'FAILED' END,
                error_message = %(error)s, claimed_by = NULL, heartbeat_at = NULL, started_at = NULL,
                updated_at = now(), {_NO_PROGRESS}
            WHERE id = %(id)s AND claimed_by = %(worker)s AND status IN {_INFLIGHT}
            RETURNING status, document_id
            """,
            {"retry": retry, "max": config.JOB_MAX_ATTEMPTS, "error": error[:500], "id": version_id,
             "worker": worker_id},
        ).fetchone()
        if row:
            _sync_document(conn, row["document_id"])
    return row["status"].lower() if row else None


def requeue_stale(stale_after_s, uploading_after_s=900):
    """Take back versions whose worker stopped sending heartbeats, and fail uploads that never finished.

    Returns how many versions were touched.
    """
    with pool().connection() as conn, conn.transaction():
        abandoned = conn.execute(
            "UPDATE document_versions SET status = 'FAILED', error_message = 'Yuklash tugamadi', updated_at = now() "
            "WHERE id IN (SELECT id FROM document_versions WHERE status = 'UPLOADED' "
            "AND updated_at < now() - make_interval(secs => %s) FOR UPDATE SKIP LOCKED) RETURNING document_id",
            (uploading_after_s,),
        ).fetchall()
        rows = conn.execute(
            f"""
            UPDATE document_versions SET
                status = CASE WHEN attempts < %(max)s THEN 'QUEUED' ELSE 'FAILED' END,
                error_message = 'Indekslash jarayoni to''xtab qoldi', claimed_by = NULL, heartbeat_at = NULL,
                started_at = NULL, updated_at = now(), {_NO_PROGRESS}
            WHERE id IN (SELECT id FROM document_versions WHERE status IN {_INFLIGHT}
                         AND heartbeat_at < now() - make_interval(secs => %(stale)s) FOR UPDATE SKIP LOCKED)
            RETURNING document_id
            """,
            {"max": config.JOB_MAX_ATTEMPTS, "stale": stale_after_s},
        ).fetchall()
        for doc_id in {r["document_id"] for r in abandoned + rows}:
            _sync_document(conn, doc_id)
    if rows:
        log.warning("took back %d version(s) from dead workers", len(rows))
    if abandoned:
        log.warning("gave up on %d upload(s) that never finished", len(abandoned))
    return len(rows) + len(abandoned)
