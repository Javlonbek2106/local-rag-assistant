"""The worker against a real PostgreSQL; only the parsing/embedding step is replaced by a fake."""
import time

import pytest

from rag import config, db, ingest, storage, store
from rag.worker import Worker

pytestmark = pytest.mark.db


class Spy:
    """Delegates to rag.db and remembers which functions were called."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        function = getattr(db, name)

        def call(*args, **kwargs):
            self.calls.append(name)
            return function(*args, **kwargs)

        return call


@pytest.fixture(autouse=True)
def fast_timers(monkeypatch, storage_dir):
    monkeypatch.setattr(config, "JOB_HEARTBEAT_S", 0.05)
    monkeypatch.setattr(config, "JOB_TIMEOUT_S", 60)
    monkeypatch.setattr(store, "count_chunks", lambda client, source: 9)


@pytest.fixture
def indexed(monkeypatch):
    """Replace parsing with a fake that records what it was asked to index."""
    seen = []

    def fake(client, converter, chunker, path, source=None):
        seen.append((path.name, source))
        return 4

    monkeypatch.setattr(ingest, "index_file", fake)
    return seen


def make_worker(repo=db, **kwargs):
    worker = Worker(repo=repo, worker_id="w1", **kwargs)
    worker.client = worker.converter = worker.chunker = object()
    return worker


def queue(tmp_path, name="a.txt", content=b"matn"):
    temp = tmp_path / f"{name}.tmp"
    temp.write_bytes(content)
    return ingest.queue_file(name, temp, len(content))


def row(name):
    return db.get_document(name)


def test_an_idle_worker_finds_nothing_to_do():
    assert make_worker().run_once() is False


def test_a_queued_document_is_indexed_under_its_id_and_marked_done(tmp_path, indexed):
    doc_id = queue(tmp_path)
    assert make_worker().run_once() is True
    assert indexed == [("a.txt", str(doc_id))]
    assert row("a.txt")["state"] == "done" and row("a.txt")["chunks"] == 4 and row("a.txt")["claimed_by"] is None


def test_an_unchanged_document_reports_the_stored_chunk_count(tmp_path, monkeypatch):
    queue(tmp_path)
    monkeypatch.setattr(ingest, "index_file", lambda *args, **kwargs: None)  # already indexed
    make_worker().run_once()
    assert row("a.txt")["chunks"] == 9


def test_an_unreadable_document_fails_at_once_without_retries(tmp_path, monkeypatch):
    queue(tmp_path)

    def unreadable(*args, **kwargs):
        raise ingest.UnreadableDocument("no text could be extracted")

    monkeypatch.setattr(ingest, "index_file", unreadable)
    make_worker().run_once()
    assert row("a.txt")["state"] == "failed" and row("a.txt")["attempts"] == 1
    assert "no text" in row("a.txt")["error"]


def test_a_missing_file_fails_at_once(tmp_path, indexed):
    doc_id = queue(tmp_path)
    storage.remove(doc_id)
    make_worker().run_once()
    assert row("a.txt")["state"] == "failed" and "topilmadi" in row("a.txt")["error"] and indexed == []


def test_a_temporary_failure_is_retried_and_finally_given_up(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOB_MAX_ATTEMPTS", 3)
    queue(tmp_path)

    def qdrant_down(*args, **kwargs):
        raise ConnectionError("qdrant is down")

    monkeypatch.setattr(ingest, "index_file", qdrant_down)
    worker = make_worker()
    states = []
    for _ in range(3):
        worker.run_once()
        states.append(row("a.txt")["state"])
    assert states == ["queued", "queued", "failed"] and row("a.txt")["attempts"] == 3
    assert worker.run_once() is False  # a failed document is left alone


def test_a_document_taken_back_from_a_dead_worker_is_processed(tmp_path, indexed):
    queue(tmp_path)
    ghost = db.claim_next("ghost")  # claimed, then the process died: no more heartbeats
    with db.pool().connection() as conn:
        conn.execute("UPDATE documents SET heartbeat_at = now() - interval '10 minutes' WHERE id = %s", (ghost["id"],))
    assert make_worker().run_once() is True  # the first poll takes it back and processes it
    assert row("a.txt")["state"] == "done" and row("a.txt")["attempts"] == 2


def test_heartbeats_are_sent_while_a_document_is_being_indexed(tmp_path, monkeypatch):
    queue(tmp_path)
    spy = Spy()

    def slow(*args, **kwargs):
        time.sleep(0.4)
        return 4

    monkeypatch.setattr(ingest, "index_file", slow)
    make_worker(repo=spy).run_once()
    assert spy.calls.count("heartbeat") >= 3
    assert row("a.txt")["state"] == "done"


def test_a_document_that_exceeds_the_time_limit_is_failed_and_the_worker_asks_to_be_restarted(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOB_TIMEOUT_S", 0.1)
    queue(tmp_path)
    restarted = []

    def stuck(*args, **kwargs):
        time.sleep(0.5)  # a parser that does not return in time
        return 4

    monkeypatch.setattr(ingest, "index_file", stuck)
    make_worker(on_timeout=lambda: restarted.append(True)).run_once()
    assert restarted == [True]
    assert row("a.txt")["state"] == "failed" and "Vaqt tugadi" in row("a.txt")["error"]
    assert row("a.txt")["attempts"] == 1  # not retried: it would only time out again


def test_a_worker_that_lost_its_claim_cannot_overwrite_the_new_owner(tmp_path, monkeypatch):
    queue(tmp_path)

    def being_replaced(*args, **kwargs):
        with db.pool().connection() as conn:  # this worker stalls; another one takes the document over
            conn.execute("UPDATE documents SET heartbeat_at = now() - interval '10 minutes'")
        db.requeue_stale(90)
        assert db.claim_next("other-worker")["claimed_by"] == "other-worker"
        time.sleep(0.2)
        return 4

    monkeypatch.setattr(ingest, "index_file", being_replaced)
    make_worker().run_once()
    found = row("a.txt")
    assert found["state"] == "processing" and found["claimed_by"] == "other-worker"  # the late result was discarded
