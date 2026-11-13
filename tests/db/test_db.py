import threading
import uuid

import psycopg
import pytest

from rag import config, db

pytestmark = pytest.mark.db


def make(name, size=10):
    """A document (a new version of it) whose file is saved and which is ready to be claimed. Returns the version id."""
    version = db.create_version(name, size, uuid.uuid4().hex)
    assert db.enqueue(version["id"])
    return version["id"]


def row(name):
    return db.get_document(name)


def version(name, number=1):
    return db.get_version(name, number)


def finish(job, worker, chunks):
    """What a worker does after indexing: verify (and record that), then activate. Returns activate()'s answer."""
    assert db.set_stage(job["id"], worker, "VERIFYING")
    assert db.record_verified(job["id"], worker, chunks)
    return db.activate(job["id"], worker, chunks)


def age_heartbeat(version_id, seconds):
    with db.pool().connection() as conn:
        conn.execute("UPDATE document_versions SET heartbeat_at = now() - make_interval(secs => %s) WHERE id = %s",
                     (seconds, version_id))


# ---------- migrations ----------

def test_migrations_are_applied_once(database):
    db.migrate()
    db.migrate()
    with db.pool().connection() as conn:
        versions = [r["version"] for r in conn.execute("SELECT version FROM schema_migrations")]
    assert versions == ["0001_documents", "0002_document_versions", "0003_rollback_generation"]


def test_concurrent_first_start_of_api_and_worker_migrates_exactly_once(monkeypatch):
    schema = f"rag_mig_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    monkeypatch.setattr(config, "DB_SCHEMA", schema)
    db.pool.cache_clear()
    errors = []

    def run():
        try:
            db.migrate()
        except Exception as error:  # noqa: BLE001 - every failure must reach the assertion
            errors.append(error)

    try:
        threads = [threading.Thread(target=run) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with db.pool().connection() as conn:
            count = conn.execute("SELECT count(*) AS n FROM schema_migrations").fetchone()["n"]
        assert errors == [] and count == 3  # each migration exactly once
    finally:
        db.close()
        with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")
        monkeypatch.undo()
        db.pool.cache_clear()


# ---------- documents ----------

def test_upload_creates_an_uploading_document_that_workers_cannot_take_yet():
    created = db.create_version("a.pdf", 123, "h")
    found = row("a.pdf")
    assert found["id"] == created["document_id"] and found["state"] == "uploading" and found["size_bytes"] == 123
    assert version("a.pdf")["attempts"] == 0 and found["chunks"] is None
    assert db.claim_next("w1") is None  # the file may still be half written
    assert db.enqueue(created["id"])
    assert db.claim_next("w1")["id"] == created["id"]


def test_enqueue_only_works_from_uploading():
    version_id = make("a.pdf")
    assert not db.enqueue(version_id)  # already queued
    assert not db.enqueue(uuid.uuid4())  # unknown id


def test_an_upload_that_never_finished_is_failed_but_a_fresh_one_is_left_alone():
    stuck, fresh = db.create_version("stuck.pdf", 1, "h1"), db.create_version("fresh.pdf", 1, "h2")
    with db.pool().connection() as conn:
        conn.execute("UPDATE document_versions SET updated_at = now() - interval '2 hours' WHERE id = %s",
                     (stuck["id"],))
    assert db.requeue_stale(90) == 1
    assert row("stuck.pdf")["state"] == "failed" and row("stuck.pdf")["error"] == "Yuklash tugamadi"
    assert row("fresh.pdf")["state"] == "uploading" and fresh["created"]


def test_a_processing_document_reports_how_long_it_has_been_running():
    make("a.pdf")
    assert row("a.pdf")["seconds"] is None
    db.claim_next("w1")
    assert row("a.pdf")["seconds"] >= 0


def test_re_upload_adds_a_version_and_keeps_the_document_and_what_it_serves():
    make("a.pdf")
    claimed = db.claim_next("w1")
    finish(claimed, "w1", chunks=5)
    second = db.create_version("a.pdf", 99, "other-hash")
    assert second["document_id"] == claimed["document_id"] and second["version_number"] == 2
    assert db.enqueue(second["id"])
    found = row("a.pdf")
    assert found["state"] == "queued" and found["size_bytes"] == 99
    assert found["version"] == 1 and found["latest_version"] == 2 and found["chunks"] == 5  # still serving version 1


def test_re_upload_while_being_indexed_is_accepted_as_the_next_version_and_changes_nothing_else():
    make("a.pdf", 10)
    db.claim_next("w1")
    second = db.create_version("a.pdf", 99, "other-hash")
    assert second["version_number"] == 2 and second["created"]
    assert version("a.pdf", 1)["status"] == "PROCESSING" and version("a.pdf", 1)["file_size"] == 10
    assert row("a.pdf")["state"] == "processing"


def test_delete_removes_idle_documents_and_refuses_busy_ones():
    make("idle.pdf")
    make("busy.pdf")
    db.claim_next("w1")  # claims the oldest: idle.pdf. Make that the busy one instead.
    assert row("idle.pdf")["state"] == "processing"
    with pytest.raises(db.Busy):
        db.delete_document("idle.pdf")
    assert db.delete_document("busy.pdf") is not None
    assert row("busy.pdf") is None
    assert db.delete_document("never-existed.pdf") is None


def test_listing_is_sorted_by_name_and_queue_length_counts_open_work():
    for name in ("c.pdf", "a.pdf", "b.pdf"):
        make(name)
    first = db.claim_next("w1")
    finish(first, "w1", chunks=1)
    assert [d["name"] for d in db.list_documents()] == ["a.pdf", "b.pdf", "c.pdf"]
    assert db.queue_length() == 2


# ---------- job queue ----------

def test_claim_takes_the_oldest_queued_document_and_marks_it_processing():
    make("first.pdf")
    make("second.pdf")
    claimed = db.claim_next("w1")
    assert claimed["name"] == "first.pdf" and claimed["status"] == "PROCESSING"
    assert claimed["claimed_by"] == "w1" and claimed["attempts"] == 1
    assert row("first.pdf")["state"] == "processing" and row("second.pdf")["state"] == "queued"
    assert db.claim_next("w2")["name"] == "second.pdf"
    assert db.claim_next("w3") is None  # nothing left


def test_parallel_workers_never_claim_the_same_document():
    ids = {make(f"doc{i}.pdf") for i in range(40)}
    claimed, lock = [], threading.Lock()

    def work(worker):
        while (job := db.claim_next(worker)) is not None:
            with lock:
                claimed.append(job["id"])

    threads = [threading.Thread(target=work, args=(f"w{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(claimed) == 40 and set(claimed) == ids  # every document exactly once


def test_only_the_claiming_worker_can_finish_a_document():
    make("a.pdf")
    job = db.claim_next("w1")
    assert not db.set_stage(job["id"], "someone-else", "VERIFYING")
    assert not db.record_verified(job["id"], "someone-else", 3)
    assert db.activate(job["id"], "someone-else", chunk_count=3) is None
    assert db.fail(job["id"], "someone-else", "boom") is None
    assert not db.heartbeat(job["id"], "someone-else")
    assert row("a.pdf")["state"] == "processing" and not row("a.pdf")["version"]
    assert finish(job, "w1", chunks=3) == "activated"
    assert row("a.pdf")["state"] == "done" and row("a.pdf")["chunks"] == 3


def test_a_failed_document_is_retried_until_attempts_run_out(monkeypatch):
    monkeypatch.setattr(config, "JOB_MAX_ATTEMPTS", 3)
    make("a.pdf")
    states = []
    for attempt in range(3):
        job = db.claim_next("w1")
        assert job["attempts"] == attempt + 1  # counted on the version: it is the job
        states.append(db.fail(job["id"], "w1", f"error {attempt}"))
    assert states == ["queued", "queued", "failed"]
    assert row("a.pdf")["state"] == "failed" and row("a.pdf")["error"] == "error 2"
    assert db.claim_next("w1") is None  # a failed document is not picked up again


def test_a_non_retryable_failure_fails_immediately():
    make("a.pdf")
    job = db.claim_next("w1")
    assert db.fail(job["id"], "w1", "no text could be extracted", retry=False) == "failed"


def test_dead_workers_documents_are_taken_back():
    make("dead.pdf")
    make("alive.pdf")
    dead, alive = db.claim_next("w1"), db.claim_next("w2")
    age_heartbeat(dead["id"], 600)
    assert db.requeue_stale(90) == 1
    assert row("dead.pdf")["state"] == "queued" and row("alive.pdf")["state"] == "processing"
    assert db.heartbeat(alive["id"], "w2")


def test_a_document_that_keeps_killing_its_worker_is_given_up_on(monkeypatch):
    monkeypatch.setattr(config, "JOB_MAX_ATTEMPTS", 2)
    make("poison.pdf")
    for _ in range(2):
        job = db.claim_next("w1")
        age_heartbeat(job["id"], 600)
        db.requeue_stale(90)
    assert row("poison.pdf")["state"] == "failed"


def test_a_replaced_worker_cannot_overwrite_the_new_result():
    make("a.pdf")
    old = db.claim_next("old-worker")
    age_heartbeat(old["id"], 600)
    db.requeue_stale(90)
    new = db.claim_next("new-worker")
    assert finish(new, "new-worker", chunks=7) == "activated"
    assert not db.set_stage(old["id"], "old-worker", "VERIFYING")  # the zombie wakes up late
    assert not db.record_verified(old["id"], "old-worker", 1)
    assert db.activate(old["id"], "old-worker", chunk_count=1) is None
    assert row("a.pdf")["chunks"] == 7
