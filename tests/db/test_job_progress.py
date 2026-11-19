"""Progress-based job timeout against a real PostgreSQL (roadmap T0.3).

Persistence of the position, the strict lease, the worker end to end (a job that moves is never killed, a stalled one
is failed, an expired worker writes nothing, retries start clean, the old version keeps serving) and the additive
migration 0004.
"""
import hashlib
import time
import uuid

import psycopg
import pytest

from rag import config, db, ingest, versioning
from rag.worker import Worker

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def fast_timers(monkeypatch, storage_dir):
    monkeypatch.setattr(config, "JOB_HEARTBEAT_S", 0.05)
    monkeypatch.setattr(config, "JOB_TIMEOUT_S", 60)
    monkeypatch.setattr(config, "JOB_STALL_S", 0.4)


@pytest.fixture(autouse=True)
def verified(monkeypatch):
    monkeypatch.setattr(versioning, "verify", lambda *args: None)


def queue(tmp_path, name="a.txt", content=b"matn"):
    temp = tmp_path / f"{name}.tmp"
    temp.write_bytes(content)
    return ingest.queue_file(name, temp, len(content))


def make_worker(**kwargs):
    worker = Worker(worker_id="w1", **kwargs)
    worker.client = worker.converter = worker.chunker = object()
    return worker


def version(name="a.txt", number=1):
    return db.get_version(name, number)


def claimed(tmp_path, name="a.txt"):
    queue(tmp_path, name)
    return db.claim_next("w1")


def expire_lease(version_id):
    with db.pool().connection() as conn:
        conn.execute("UPDATE document_versions SET heartbeat_at = now() - interval '10 minutes' WHERE id = %s",
                     (version_id,))


def columns(version_id):
    with db.pool().connection() as conn:
        return conn.execute("SELECT progress_unit, progress_done, progress_total, progress_at FROM document_versions "
                            "WHERE id = %s", (version_id,)).fetchone()


# ---------- the position is persisted and shown ----------

def test_a_position_is_stored_and_shown_wherever_versions_and_documents_are_read(tmp_path):
    job = claimed(tmp_path)
    assert columns(job["id"]) == {"progress_unit": None, "progress_done": None, "progress_total": None,
                                  "progress_at": None}  # a claim starts without a position
    assert db.set_progress(job["id"], "w1", "pages", 120, 381)
    stored = columns(job["id"])
    assert (stored["progress_unit"], stored["progress_done"], stored["progress_total"]) == ("pages", 120, 381)
    assert stored["progress_at"] is not None

    document = db.get_document("a.txt")
    assert document["stage"] == "PROCESSING" and document["state"] == "processing"
    assert (document["progress_unit"], document["progress_done"], document["progress_total"]) == ("pages", 120, 381)
    assert db.list_documents()[0]["progress_done"] == 120
    assert db.get_version("a.txt", 1)["progress_total"] == 381 and db.list_versions("a.txt")[0]["progress_done"] == 120
    [running] = db.status_snapshot()["in_flight"]
    assert (running["progress_unit"], running["progress_done"], running["progress_total"]) == ("pages", 120, 381)
    assert running["progress_age_seconds"] == 0


def test_the_total_may_be_unknown(tmp_path):
    job = claimed(tmp_path)
    assert db.set_progress(job["id"], "w1", "chunks", 50)
    assert columns(job["id"])["progress_total"] is None


def test_a_new_stage_clears_the_position(tmp_path):
    job = claimed(tmp_path)
    db.set_progress(job["id"], "w1", "pages", 5, 10)
    assert db.set_stage(job["id"], "w1", "EMBEDDING")
    assert columns(job["id"]) == {"progress_unit": None, "progress_done": None, "progress_total": None,
                                  "progress_at": None}
    assert db.get_document("a.txt")["stage"] == "EMBEDDING" and db.get_document("a.txt")["progress_unit"] is None


def test_the_position_cannot_be_negative(tmp_path):
    job = claimed(tmp_path)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.set_progress(job["id"], "w1", "pages", -1, 10)


# ---------- the strict lease: only the worker that holds the version, and only while its lease is valid ----------

def test_another_worker_cannot_write_a_position(tmp_path):
    job = claimed(tmp_path)
    assert db.set_progress(job["id"], "someone-else", "pages", 9, 10) is False
    assert columns(job["id"])["progress_unit"] is None


def test_a_worker_whose_lease_has_expired_cannot_write_a_position_even_if_nobody_took_over(tmp_path):
    job = claimed(tmp_path)
    db.set_progress(job["id"], "w1", "pages", 3, 10)
    expire_lease(job["id"])
    assert db.set_progress(job["id"], "w1", "pages", 4, 10) is False
    assert columns(job["id"])["progress_done"] == 3  # the late write changed nothing
    assert db.heartbeat(job["id"], "w1") is False  # and an expired lease is still not renewed


def test_reporting_a_position_does_not_renew_the_lease(tmp_path):
    job = claimed(tmp_path)
    with db.pool().connection() as conn:
        conn.execute("UPDATE document_versions SET heartbeat_at = now() - interval '30 seconds' WHERE id = %s",
                     (job["id"],))
        before = conn.execute("SELECT heartbeat_at FROM document_versions WHERE id = %s", (job["id"],)).fetchone()
    assert db.set_progress(job["id"], "w1", "pages", 1, 2)
    with db.pool().connection() as conn:
        after = conn.execute("SELECT heartbeat_at FROM document_versions WHERE id = %s", (job["id"],)).fetchone()
    assert after == before  # only heartbeat() and set_stage() renew it: progress cannot keep a dead worker alive


def test_nothing_can_be_written_to_a_version_nobody_holds(tmp_path):
    queued = queue(tmp_path)
    assert db.set_progress(queued["id"], "w1", "pages", 1, 2) is False  # still QUEUED


# ---------- the position never outlives the job ----------

def has_position(version_id):
    return columns(version_id)["progress_unit"] is not None


def test_a_failed_attempt_that_will_be_retried_leaves_no_position(tmp_path):
    job = claimed(tmp_path)
    db.set_progress(job["id"], "w1", "pages", 5, 10)
    assert db.fail(job["id"], "w1", "qdrant down", retry=True) == "queued"
    assert not has_position(job["id"]) and db.get_document("a.txt")["progress_unit"] is None


def test_a_given_up_job_leaves_no_position(tmp_path):
    job = claimed(tmp_path)
    db.set_progress(job["id"], "w1", "pages", 5, 10)
    assert db.fail(job["id"], "w1", "no text", retry=False) == "failed"
    assert not has_position(job["id"])


def test_a_job_taken_back_from_a_dead_worker_leaves_no_position(tmp_path):
    job = claimed(tmp_path)
    db.set_progress(job["id"], "w1", "pages", 5, 10)
    expire_lease(job["id"])
    assert db.requeue_stale(90) == 1
    assert not has_position(job["id"])


def test_a_finished_version_leaves_no_position(tmp_path):
    job = claimed(tmp_path)
    db.set_progress(job["id"], "w1", "pages", 10, 10)
    assert db.set_stage(job["id"], "w1", "VERIFYING") and db.record_verified(job["id"], "w1", 3)
    db.set_progress(job["id"], "w1", "chunks", 3, 3)
    assert db.activate(job["id"], "w1", 3) == "activated"
    assert not has_position(job["id"]) and db.get_document("a.txt")["stage"] is None


def test_a_claim_never_inherits_a_position(tmp_path):
    queued = queue(tmp_path)
    with db.pool().connection() as conn:  # a leftover that should not exist, planted on purpose
        conn.execute("UPDATE document_versions SET progress_unit = 'pages', progress_done = 9, progress_total = 9 "
                     "WHERE id = %s", (queued["id"],))
    db.claim_next("w1")
    assert not has_position(queued["id"])


# ---------- the worker end to end ----------

def test_a_big_job_that_keeps_moving_is_not_stopped_and_its_position_is_in_the_database_while_it_runs(tmp_path,
                                                                                                    monkeypatch):
    queue(tmp_path)
    seen = []

    def big(client, converter, chunker, path, *, document_id, version_id, file_name, file_hash, stage):
        for page in range(1, 31):  # 1.5 s of work against a 0.4 s stall limit
            stage.progress("pages", page, 30)
            if page in (10, 30):
                seen.append((db.get_document("a.txt")["stage"], db.get_document("a.txt")["progress_done"]))
            time.sleep(0.05)
        stage("EMBEDDING")
        stage.progress("chunks", 3, 3)
        stage("INDEXING")
        return 3

    monkeypatch.setattr(ingest, "index_version", big)
    restarted = []
    started = time.monotonic()
    make_worker(on_timeout=lambda: restarted.append(True)).run_once()
    assert time.monotonic() - started > 3 * config.JOB_STALL_S  # it really ran much longer than the stall limit
    assert restarted == [] and version()["status"] == "READY" and db.get_document("a.txt")["version"] == 1
    assert seen == [("PROCESSING", 10), ("PROCESSING", 30)]  # PostgreSQL held the position while the job ran


def test_a_stalled_job_is_failed_with_its_last_position_and_the_old_version_keeps_serving(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "index_version", lambda *args, **kwargs: 2)
    queue(tmp_path, content=b"one")
    make_worker().run_once()
    old = version()
    queue(tmp_path, content=b"two")

    def stalls(client, converter, chunker, path, *, document_id, version_id, file_name, file_hash, stage):
        stage.progress("pages", 7, 20)
        assert db.active_version_ids() == [str(old["id"])]  # the old version is what searches see meanwhile
        time.sleep(1.5)  # a parser that never returns (heartbeats go on; progress does not)
        return 4

    monkeypatch.setattr(ingest, "index_version", stalls)
    restarted = []
    make_worker(on_timeout=lambda: restarted.append(True)).run_once()
    new = version(number=2)
    assert restarted == [True]
    assert new["status"] == "FAILED" and new["attempts"] == 1  # not retried: it would only stall again
    assert new["error_message"].startswith("Vaqt tugadi") and "sahifa 7/20" in new["error_message"]
    assert columns(new["id"])["progress_unit"] is None
    assert db.active_version_ids() == [str(old["id"])]
    found = db.get_document("a.txt")
    assert found["state"] == "done" and found["version"] == 1 and found["chunks"] == 2  # still fully usable
    assert "Vaqt tugadi" in found["update_error"]
    assert version()["status"] == "READY" and version()["activated_at"] == old["activated_at"]


def test_a_job_that_moves_forever_is_still_stopped_by_the_ceiling(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOB_TIMEOUT_S", 0.4)
    queue(tmp_path)

    def endless(client, converter, chunker, path, *, document_id, version_id, file_name, file_hash, stage):
        for n in range(100):
            stage.progress("chunks", n, None)
            time.sleep(0.05)
        return 1

    monkeypatch.setattr(ingest, "index_version", endless)
    restarted = []
    make_worker(on_timeout=lambda: restarted.append(True)).run_once()
    assert restarted == [True] and "jami chegara" in version()["error_message"] and version()["attempts"] == 1


def test_a_worker_whose_lease_expired_cannot_continue_writing_and_its_result_is_discarded(tmp_path, monkeypatch):
    queue(tmp_path)
    outcome = []

    def expired(client, converter, chunker, path, *, document_id, version_id, file_name, file_hash, stage):
        stage.progress("pages", 3, 10)
        expire_lease(version_id)  # this worker stalled past its lease; nobody has taken over yet
        try:
            stage.progress("pages", 4, 10)
        except Exception as error:  # noqa: BLE001
            outcome.append(type(error).__name__)
            raise
        return 4

    monkeypatch.setattr(ingest, "index_version", expired)
    make_worker().run_once()
    found = version()
    assert outcome == ["ClaimLost"]
    assert found["progress_done"] == 3  # the write after the expiry changed nothing
    assert found["status"] == "PROCESSING" and db.get_document("a.txt")["version"] is None  # nothing was finished


def test_a_retry_starts_from_a_clean_position_and_finishes_once(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOB_MAX_ATTEMPTS", 3)
    queue(tmp_path)
    attempts = []

    def flaky(client, converter, chunker, path, *, document_id, version_id, file_name, file_hash, stage):
        attempts.append(columns(version_id)["progress_unit"])  # what this attempt finds when it starts
        stage.progress("pages", 5, 10)
        if len(attempts) == 1:
            raise ConnectionError("qdrant is down")
        return 3

    monkeypatch.setattr(ingest, "index_version", flaky)
    worker = make_worker()
    worker.run_once()
    assert db.get_document("a.txt")["state"] == "queued" and columns(version()["id"])["progress_unit"] is None
    worker.run_once()
    assert attempts == [None, None]  # the second attempt did not see the first one's position
    assert version()["status"] == "READY" and version()["attempts"] == 2 and db.get_document("a.txt")["chunks"] == 3


# ---------- migration 0004 is additive ----------

def test_the_migration_adds_columns_and_changes_nothing_that_was_there():
    schema = f"rag_mig4_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    original_schema = config.DB_SCHEMA
    config.DB_SCHEMA = schema
    db.pool.cache_clear()
    try:
        with db.pool().connection() as conn:  # the database as it was before this migration
            conn.execute("CREATE TABLE schema_migrations (version text PRIMARY KEY, "
                         "applied_at timestamptz NOT NULL DEFAULT now())")
            for name in ("0001_documents", "0002_document_versions", "0003_rollback_generation"):
                conn.execute((db.MIGRATIONS / f"{name}.sql").read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (name,))
            for n, status in enumerate(("QUEUED", "PROCESSING", "EMBEDDING", "READY", "FAILED", "SUPERSEDED")):
                doc = conn.execute("INSERT INTO documents (name, size_bytes, state) VALUES (%s, 10, 'processing') "
                                   "RETURNING id", (f"d{n}.pdf",)).fetchone()["id"]
                conn.execute(
                    "INSERT INTO document_versions (document_id, version_number, file_name, file_hash, file_size, "
                    "file_path, status, chunk_count, error_message, attempts, claimed_by, heartbeat_at, started_at) "
                    "VALUES (%s, 1, %s, %s, 10, %s, %s, %s, %s, 1, 'w', now(), now())",
                    (doc, f"d{n}.pdf", hashlib.sha256(str(n).encode()).hexdigest(), f"{doc}/d{n}.pdf", status,
                     n if status == "READY" else None, "old error" if status == "FAILED" else None))
            old_columns = "id, document_id, version_number, file_name, file_hash, file_size, file_path, status, " \
                          "chunk_count, error_message, attempts, claimed_by, heartbeat_at, started_at, created_at, " \
                          "updated_at, indexed_at, activated_at, uploaded_generation"
            before = conn.execute(f"SELECT {old_columns} FROM document_versions ORDER BY id").fetchall()
            documents_before = conn.execute("SELECT * FROM documents ORDER BY id").fetchall()
            existing = {r["column_name"] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = %s "
                "AND table_name IN ('documents', 'document_versions')", (schema,))}

        db.migrate()  # applies 0004, and every migration written after it (what this test is about is 0004)

        with db.pool().connection() as conn:
            applied = [r["version"] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
            assert applied[:4] == ["0001_documents", "0002_document_versions", "0003_rollback_generation",
                                   "0004_job_progress"] and "0004_job_progress" in applied
            assert conn.execute(f"SELECT {old_columns} FROM document_versions ORDER BY id").fetchall() == before
            assert conn.execute("SELECT * FROM documents ORDER BY id").fetchall() == documents_before
            now = {r["column_name"] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = %s "
                "AND table_name IN ('documents', 'document_versions')", (schema,))}
            assert now - existing == {"progress_unit", "progress_done", "progress_total", "progress_at"}
            assert existing <= now  # nothing was removed or renamed
            assert conn.execute("SELECT count(*) AS n FROM document_versions WHERE progress_unit IS NOT NULL OR "
                                "progress_done IS NOT NULL OR progress_total IS NOT NULL OR progress_at IS NOT NULL"
                                ).fetchone()["n"] == 0  # the new columns start empty, even for jobs in flight
        job = db.claim_next("w1")  # and the queue works on the migrated data
        assert job["status"] == "PROCESSING" and job["progress_unit"] is None
    finally:
        db.close()
        config.DB_SCHEMA = original_schema
        db.pool.cache_clear()
        with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")
