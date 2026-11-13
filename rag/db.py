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
import uuid
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from rag import config, storage

log = logging.getLogger(__name__)
MIGRATIONS = Path(__file__).parent / "migrations"
MIGRATION_LOCK = 727001  # arbitrary key for pg_advisory_lock: api and worker may start at the same moment
INFLIGHT = ("PROCESSING", "EMBEDDING", "INDEXING", "VERIFYING")  # a worker holds the version
_INFLIGHT = "('PROCESSING', 'EMBEDDING', 'INDEXING', 'VERIFYING')"
DOCUMENT_SELECT = f"""
    SELECT d.id, d.name, d.size_bytes, d.state, d.chunks, d.activation_generation,
           CASE WHEN d.state = 'failed' THEN d.error END AS error,
           CASE WHEN d.state = 'done' THEN d.error END AS update_error,
           d.created_at, d.updated_at, av.version_number AS version,
           (SELECT max(x.version_number) FROM document_versions x WHERE x.document_id = d.id) AS latest_version,
           (SELECT extract(epoch FROM now() - x.started_at)::int FROM document_versions x
             WHERE x.document_id = d.id AND x.status IN {_INFLIGHT} ORDER BY x.version_number DESC LIMIT 1) AS seconds
    FROM documents d LEFT JOIN document_versions av ON av.id = d.active_version_id
"""
VERSION_COLUMNS = f"""
    v.id, v.document_id, d.name, v.version_number, v.file_name, v.file_hash, v.file_size, v.file_path, v.status,
    v.chunk_count, v.error_message, v.attempts, v.claimed_by, v.created_at, v.indexed_at, v.activated_at,
    v.uploaded_generation, d.activation_generation,
    (d.active_version_id = v.id) AS active,
    CASE WHEN v.status IN {_INFLIGHT} THEN extract(epoch FROM now() - v.started_at)::int END AS seconds
"""


class Busy(Exception):
    """The document is being indexed right now, so it cannot be deleted."""


class VersionConflict(Exception):
    """The requested change of the active version is not allowed in the version's current state."""


@functools.lru_cache(maxsize=1)
def pool():
    kwargs = {"row_factory": dict_row, "autocommit": True}
    if config.DB_SCHEMA:
        kwargs["options"] = f"-c search_path={config.DB_SCHEMA}"
    return ConnectionPool(config.DATABASE_URL, min_size=1, max_size=config.DB_POOL_SIZE, kwargs=kwargs, open=True)


def close():
    pool().close()
    pool.cache_clear()


def migrate():
    """Apply every migrations/*.sql that has not been applied yet. Safe to call from several processes."""
    with pool().connection() as conn:
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
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK,))




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
    newest version that did not fail creates nothing (created=False) and returns that version.
    """
    with pool().connection() as conn, conn.transaction():
        doc = conn.execute(
            "INSERT INTO documents (name, size_bytes, state) VALUES (%s, %s, 'uploading') "
            "ON CONFLICT (name) DO UPDATE SET updated_at = now() "
            "RETURNING id, activation_generation",  # the update also locks the row
            (name, size_bytes),
        ).fetchone()
        newest = conn.execute(
            "SELECT id, version_number, file_hash, file_path FROM document_versions "
            "WHERE document_id = %s AND status <> 'FAILED' ORDER BY version_number DESC LIMIT 1", (doc["id"],),
        ).fetchone()
        if newest and newest["file_hash"] == file_hash:
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
                       chunk_count = NULL, started_at = now(), heartbeat_at = now(), updated_at = now()
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
    """Say "still alive". False means the claim was lost (this worker was declared dead and replaced)."""
    with pool().connection() as conn:
        return conn.execute(
            f"UPDATE document_versions SET heartbeat_at = now() WHERE id = %s AND claimed_by = %s "
            f"AND status IN {_INFLIGHT} RETURNING id", (version_id, worker_id),
        ).fetchone() is not None


def set_stage(version_id, worker_id, status):
    """Move a held version to the next stage (PROCESSING, EMBEDDING, INDEXING, VERIFYING). False if the claim was lost.
    The document stays 'processing' through all of them, so its summary does not change."""
    if status not in INFLIGHT:
        raise ValueError(status)
    with pool().connection() as conn:
        return conn.execute(
            f"UPDATE document_versions SET status = %s, heartbeat_at = now(), updated_at = now() "
            f"WHERE id = %s AND claimed_by = %s AND status IN {_INFLIGHT} RETURNING id",
            (status, version_id, worker_id),
        ).fetchone() is not None


def record_verified(version_id, worker_id, chunk_count):
    """Record that the version's index was checked and found complete (rag.versioning.verify passed): the chunk count
    is written only now, and activate() accepts nothing without it. False if the claim was lost."""
    with pool().connection() as conn:
        return conn.execute(
            "UPDATE document_versions SET chunk_count = %s, updated_at = now() "
            "WHERE id = %s AND claimed_by = %s AND status = 'VERIFYING' RETURNING id",
            (chunk_count, version_id, worker_id),
        ).fetchone() is not None


def activate(version_id, worker_id, chunk_count):
    """Make a verified version the active one, in one transaction. Returns "activated", "superseded" (the version is
    only kept, for a later rollback or roll-forward) or None (the claim was lost or the version was not verified;
    nothing changed).

    Steps: lock this version, then its document, then the active version; confirm this worker still holds the claim,
    the version is VERIFYING and its verified chunk count was recorded (record_verified) and matches; then decide:

    - a rollback happened after this version was uploaded (uploaded_generation < the document's activation_generation):
      the user chose a version since, and this late candidate must not replace that choice -> "superseded";
    - a newer version is already active -> "superseded";
    - otherwise demote the old active version, promote this one and refresh the document summary -> "activated".

    The generation is compared under the document lock, which a rollback also takes, so a rollback either commits
    before this check (and wins) or after this activation (and then overrides it, as the later decision).
    """
    with pool().connection() as conn, conn.transaction():
        mine = conn.execute(
            "SELECT id, document_id, version_number, status, claimed_by, chunk_count, uploaded_generation "
            "FROM document_versions WHERE id = %s FOR UPDATE", (version_id,),
        ).fetchone()
        if (mine is None or mine["claimed_by"] != worker_id or mine["status"] != "VERIFYING"
                or mine["chunk_count"] != chunk_count):  # chunk_count is NULL until record_verified
            return None
        doc = conn.execute("SELECT active_version_id, activation_generation FROM documents WHERE id = %s FOR UPDATE",
                           (mine["document_id"],)).fetchone()
        current = None
        if doc["active_version_id"]:
            current = conn.execute("SELECT id, version_number FROM document_versions WHERE id = %s FOR UPDATE",
                                   (doc["active_version_id"],)).fetchone()
        rolled_back_since = mine["uploaded_generation"] < doc["activation_generation"]
        released = "claimed_by = NULL, heartbeat_at = NULL, started_at = NULL, error_message = NULL, updated_at = now()"
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
                updated_at = now()
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
                started_at = NULL, updated_at = now()
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
