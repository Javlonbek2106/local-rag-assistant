import threading
import uuid

import psycopg
import pytest

from rag import config, db

pytestmark = pytest.mark.db


def make(name, size=10):
    """A document whose file is saved and which is ready to be claimed."""
    doc_id = db.upsert_document(name, size)
    assert db.enqueue(doc_id)
    return doc_id


def row(name):
    return db.get_document(name)


def age_heartbeat(doc_id, seconds):
    with db.pool().connection() as conn:
        conn.execute("UPDATE documents SET heartbeat_at = now() - make_interval(secs => %s) WHERE id = %s",
                     (seconds, doc_id))


# ---------- migrations ----------

def test_migrations_are_applied_once(database):
    db.migrate()
    db.migrate()
    with db.pool().connection() as conn:
        versions = [r["version"] for r in conn.execute("SELECT version FROM schema_migrations")]
    assert versions == ["0001_documents"]


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
        assert errors == [] and count == 1
    finally:
        db.close()
        with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")
        monkeypatch.undo()
        db.pool.cache_clear()


# ---------- documents ----------

def test_upload_creates_an_uploading_document_that_workers_cannot_take_yet():
    doc_id = db.upsert_document("a.pdf", 123)
    found = row("a.pdf")
    assert found["id"] == doc_id and found["state"] == "uploading" and found["size_bytes"] == 123
    assert found["attempts"] == 0 and found["chunks"] is None
    assert db.claim_next("w1") is None  # the file may still be half written
    assert db.enqueue(doc_id)
    assert db.claim_next("w1")["id"] == doc_id


def test_enqueue_only_works_from_uploading():
    doc_id = make("a.pdf")
    assert not db.enqueue(doc_id)  # already queued
    assert not db.enqueue(uuid.uuid4())  # unknown id


def test_an_upload_that_never_finished_is_failed_but_a_fresh_one_is_left_alone():
    stuck, fresh = db.upsert_document("stuck.pdf", 1), db.upsert_document("fresh.pdf", 1)
    with db.pool().connection() as conn:
        conn.execute("UPDATE documents SET updated_at = now() - interval '2 hours' WHERE id = %s", (stuck,))
    assert db.requeue_stale(90) == 1
    assert row("stuck.pdf")["state"] == "failed" and row("stuck.pdf")["error"] == "Yuklash tugamadi"
    assert row("fresh.pdf")["state"] == "uploading" and fresh


def test_a_processing_document_reports_how_long_it_has_been_running():
    make("a.pdf")
    assert row("a.pdf")["seconds"] is None
    db.claim_next("w1")
    assert row("a.pdf")["seconds"] >= 0


def test_re_upload_resets_the_document_but_keeps_its_id():
    first = make("a.pdf")
    claimed = db.claim_next("w1")
    db.complete(claimed["id"], "w1", chunks=5)
    assert make("a.pdf", 99) == first
    found = row("a.pdf")
    assert found["state"] == "queued" and found["chunks"] is None and found["attempts"] == 0
    assert found["size_bytes"] == 99


def test_re_upload_while_being_indexed_is_refused_and_changes_nothing():
    make("a.pdf", 10)
    db.claim_next("w1")
    with pytest.raises(db.Busy):
        make("a.pdf", 99)
    assert row("a.pdf")["state"] == "processing" and row("a.pdf")["size_bytes"] == 10


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
    db.complete(first["id"], "w1", chunks=1)
    assert [d["name"] for d in db.list_documents()] == ["a.pdf", "b.pdf", "c.pdf"]
    assert db.queue_length() == 2


# ---------- job queue ----------

def test_claim_takes_the_oldest_queued_document_and_marks_it_processing():
    make("first.pdf")
    make("second.pdf")
    claimed = db.claim_next("w1")
    assert claimed["name"] == "first.pdf" and claimed["state"] == "processing"
    assert claimed["claimed_by"] == "w1" and claimed["attempts"] == 1
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
    assert not db.complete(job["id"], "someone-else", chunks=3)
    assert db.fail(job["id"], "someone-else", "boom") is None
    assert not db.heartbeat(job["id"], "someone-else")
    assert row("a.pdf")["state"] == "processing"
    assert db.complete(job["id"], "w1", chunks=3)
    assert row("a.pdf")["state"] == "done" and row("a.pdf")["chunks"] == 3


def test_a_failed_document_is_retried_until_attempts_run_out(monkeypatch):
    monkeypatch.setattr(config, "JOB_MAX_ATTEMPTS", 3)
    make("a.pdf")
    states = []
    for attempt in range(3):
        job = db.claim_next("w1")
        assert job["attempts"] == attempt + 1
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
    assert db.complete(new["id"], "new-worker", chunks=7)
    assert not db.complete(old["id"], "old-worker", chunks=1)  # the zombie wakes up late
    assert row("a.pdf")["chunks"] == 7
