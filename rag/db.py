"""PostgreSQL: the durable record of documents and the indexing job queue.

A document row is its own job. Workers claim `queued` rows with FOR UPDATE SKIP LOCKED, keep a heartbeat while
they work and finish with complete() or fail(). Every finishing call checks `claimed_by`, so a worker that was
declared dead and replaced can never overwrite the result of the worker that took over.
"""
import functools
import logging
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from rag import config

log = logging.getLogger(__name__)
MIGRATIONS = Path(__file__).parent / "migrations"
MIGRATION_LOCK = 727001  # arbitrary key for pg_advisory_lock: api and worker may start at the same moment
COLUMNS = (
    "id, name, size_bytes, state, chunks, error, attempts, claimed_by, started_at, created_at, updated_at, "
    "CASE WHEN state = 'processing' THEN extract(epoch FROM now() - started_at)::int END AS seconds"
)


class Busy(Exception):
    """The document is being indexed right now, so it cannot be replaced or deleted."""


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


# ---------- documents ----------

def upsert_document(name, size_bytes):
    """Create the document as 'uploading', or reset the existing one for a re-upload. Returns its id.

    The caller saves the file and then calls enqueue(). Raises Busy when that document is being indexed right now.
    """
    with pool().connection() as conn:
        found = conn.execute(
            """
            INSERT INTO documents (name, size_bytes, state) VALUES (%s, %s, 'uploading')
            ON CONFLICT (name) DO UPDATE SET
                size_bytes = EXCLUDED.size_bytes, state = 'uploading', chunks = NULL, error = NULL, attempts = 0,
                claimed_by = NULL, heartbeat_at = NULL, started_at = NULL, updated_at = now()
            WHERE documents.state <> 'processing'
            RETURNING id
            """,
            (name, size_bytes),
        ).fetchone()
    if found is None:
        raise Busy(name)
    return found["id"]


def enqueue(doc_id):
    """The file is saved: let workers take it. False if the document is no longer 'uploading'."""
    with pool().connection() as conn:
        return conn.execute(
            "UPDATE documents SET state = 'queued', updated_at = now() WHERE id = %s AND state = 'uploading' "
            "RETURNING id", (doc_id,),
        ).fetchone() is not None


def get_document(name):
    with pool().connection() as conn:
        return conn.execute(f"SELECT {COLUMNS} FROM documents WHERE name = %s", (name,)).fetchone()


def list_documents():
    with pool().connection() as conn:
        return conn.execute(f"SELECT {COLUMNS} FROM documents ORDER BY name").fetchall()


def queue_length():
    with pool().connection() as conn:
        sql = "SELECT count(*) AS n FROM documents WHERE state IN ('queued', 'processing')"
        return conn.execute(sql).fetchone()["n"]


def delete_document(name):
    """Delete the row and return its id; None if there is no such document. Raises Busy while it is indexed."""
    with pool().connection() as conn:
        deleted = conn.execute(
            "DELETE FROM documents WHERE name = %s AND state <> 'processing' RETURNING id", (name,)
        ).fetchone()
        if deleted:
            return deleted["id"]
        if conn.execute("SELECT 1 FROM documents WHERE name = %s", (name,)).fetchone():
            raise Busy(name)
    return None


# ---------- job queue ----------

def claim_next(worker_id):
    """Take the oldest queued document for this worker, or None. Two workers never get the same one."""
    with pool().connection() as conn:
        return conn.execute(
            f"""
            UPDATE documents SET state = 'processing', claimed_by = %s, attempts = attempts + 1,
                   started_at = now(), heartbeat_at = now(), updated_at = now()
            WHERE id = (SELECT id FROM documents WHERE state = 'queued'
                        ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)
            RETURNING {COLUMNS}
            """,
            (worker_id,),
        ).fetchone()


def heartbeat(doc_id, worker_id):
    """Say "still alive". False means the claim was lost (this worker was declared dead and replaced)."""
    with pool().connection() as conn:
        return conn.execute(
            "UPDATE documents SET heartbeat_at = now() WHERE id = %s AND claimed_by = %s AND state = 'processing' "
            "RETURNING id", (doc_id, worker_id),
        ).fetchone() is not None


def complete(doc_id, worker_id, chunks):
    """Mark done. False if this worker no longer holds the claim (its result must be discarded)."""
    with pool().connection() as conn:
        return conn.execute(
            "UPDATE documents SET state = 'done', chunks = %s, error = NULL, claimed_by = NULL, heartbeat_at = NULL, "
            "updated_at = now() WHERE id = %s AND claimed_by = %s AND state = 'processing' RETURNING id",
            (chunks, doc_id, worker_id),
        ).fetchone() is not None


def fail(doc_id, worker_id, error, retry=True):
    """Record a failure. Returns the new state ('queued' to retry, 'failed' when out of attempts), or None if the
    claim was lost."""
    with pool().connection() as conn:
        row = conn.execute(
            """
            UPDATE documents SET
                state = CASE WHEN %(retry)s AND attempts < %(max)s THEN 'queued' ELSE 'failed' END,
                error = %(error)s, claimed_by = NULL, heartbeat_at = NULL, started_at = NULL, updated_at = now()
            WHERE id = %(id)s AND claimed_by = %(worker)s AND state = 'processing'
            RETURNING state
            """,
            {"retry": retry, "max": config.JOB_MAX_ATTEMPTS, "error": error[:500], "id": doc_id, "worker": worker_id},
        ).fetchone()
    return row["state"] if row else None


def requeue_stale(stale_after_s, uploading_after_s=900):
    """Take back documents whose worker stopped sending heartbeats, and fail uploads that never finished.

    Returns how many documents were touched.
    """
    with pool().connection() as conn:
        abandoned = conn.execute(
            "UPDATE documents SET state = 'failed', error = 'Yuklash tugamadi', updated_at = now() "
            "WHERE state = 'uploading' AND updated_at < now() - make_interval(secs => %s) RETURNING id",
            (uploading_after_s,),
        ).fetchall()
        rows = conn.execute(
            """
            UPDATE documents SET
                state = CASE WHEN attempts < %(max)s THEN 'queued' ELSE 'failed' END,
                error = 'Indekslash jarayoni to''xtab qoldi', claimed_by = NULL, heartbeat_at = NULL,
                started_at = NULL, updated_at = now()
            WHERE state = 'processing' AND heartbeat_at < now() - make_interval(secs => %(stale)s)
            RETURNING id
            """,
            {"max": config.JOB_MAX_ATTEMPTS, "stale": stale_after_s},
        ).fetchall()
    if rows:
        log.warning("took back %d document(s) from dead workers", len(rows))
    if abandoned:
        log.warning("gave up on %d upload(s) that never finished", len(abandoned))
    return len(rows) + len(abandoned)
