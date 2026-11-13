"""The worker against a real PostgreSQL; only the parsing/embedding step (and Qdrant) is replaced by a fake."""
import time

import pytest

from rag import config, db, ingest, storage, versioning
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


@pytest.fixture
def verified(monkeypatch):
    """Verification against Qdrant is replaced by a recorder that always agrees."""
    seen = []
    monkeypatch.setattr(versioning, "verify", lambda client, doc, ver, file_hash, expected: seen.append(expected))
    return seen


@pytest.fixture
def indexed(monkeypatch, verified):
    """Replace parsing with a fake that records what it was asked to index and the statuses it passed through."""
    seen = []

    def fake(client, converter, chunker, path, *, document_id, version_id, file_name, file_hash, stage):
        seen.append((file_name, str(document_id)))
        stage("EMBEDDING")
        stage("INDEXING")
        return 4

    monkeypatch.setattr(ingest, "index_version", fake)
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


def version(name, number=1):
    return db.get_version(name, number)


def test_an_idle_worker_finds_nothing_to_do():
    assert make_worker().run_once() is False


def test_a_queued_document_is_indexed_under_its_id_and_marked_done(tmp_path, indexed, verified):
    queued = queue(tmp_path)
    assert make_worker().run_once() is True
    assert indexed == [("a.txt", str(queued["document_id"]))]
    assert verified == [4]  # the version was verified against the number of chunks it wrote
    assert row("a.txt")["state"] == "done" and row("a.txt")["chunks"] == 4 and row("a.txt")["version"] == 1
    assert version("a.txt")["status"] == "READY" and version("a.txt")["claimed_by"] is None


def test_an_unchanged_re_upload_is_not_indexed_again(tmp_path, indexed):
    queue(tmp_path)
    make_worker().run_once()
    assert queue(tmp_path)["created"] is False  # same bytes: no new version, nothing for the worker to do
    assert make_worker().run_once() is False and len(indexed) == 1


def test_an_unreadable_document_fails_at_once_without_retries(tmp_path, monkeypatch, verified):
    queue(tmp_path)

    def unreadable(*args, **kwargs):
        raise ingest.UnreadableDocument("no text could be extracted")

    monkeypatch.setattr(ingest, "index_version", unreadable)
    make_worker().run_once()
    assert row("a.txt")["state"] == "failed" and version("a.txt")["attempts"] == 1
    assert "no text" in row("a.txt")["error"]


def test_a_missing_file_fails_at_once(tmp_path, indexed):
    queued = queue(tmp_path)
    storage.remove(queued["document_id"])
    make_worker().run_once()
    assert row("a.txt")["state"] == "failed" and "topilmadi" in row("a.txt")["error"] and indexed == []


def test_a_temporary_failure_is_retried_and_finally_given_up(tmp_path, monkeypatch, verified):
    monkeypatch.setattr(config, "JOB_MAX_ATTEMPTS", 3)
    queue(tmp_path)

    def qdrant_down(*args, **kwargs):
        raise ConnectionError("qdrant is down")

    monkeypatch.setattr(ingest, "index_version", qdrant_down)
    worker = make_worker()
    states = []
    for _ in range(3):
        worker.run_once()
        states.append(row("a.txt")["state"])
    assert states == ["queued", "queued", "failed"] and version("a.txt")["attempts"] == 3
    assert worker.run_once() is False  # a failed document is left alone


def test_a_document_taken_back_from_a_dead_worker_is_processed(tmp_path, indexed):
    queue(tmp_path)
    ghost = db.claim_next("ghost")  # claimed, then the process died: no more heartbeats
    with db.pool().connection() as conn:
        conn.execute("UPDATE document_versions SET heartbeat_at = now() - interval '10 minutes' WHERE id = %s",
                     (ghost["id"],))
    assert make_worker().run_once() is True  # the first poll takes it back and processes it
    assert row("a.txt")["state"] == "done" and version("a.txt")["attempts"] == 2


def test_heartbeats_are_sent_while_a_document_is_being_indexed(tmp_path, monkeypatch, verified):
    queue(tmp_path)
    spy = Spy()

    def slow(*args, **kwargs):
        time.sleep(0.4)
        return 4

    monkeypatch.setattr(ingest, "index_version", slow)
    worker = make_worker(repo=spy)
    worker.run_once()
    assert spy.calls.count("heartbeat") >= 3
    assert row("a.txt")["state"] == "done"


def test_a_document_that_exceeds_the_time_limit_is_failed_and_the_worker_asks_to_be_restarted(tmp_path, monkeypatch,
                                                                                              verified):
    monkeypatch.setattr(config, "JOB_TIMEOUT_S", 0.1)
    queue(tmp_path)
    restarted = []

    def stuck(*args, **kwargs):
        time.sleep(0.5)  # a parser that does not return in time
        return 4

    monkeypatch.setattr(ingest, "index_version", stuck)
    make_worker(on_timeout=lambda: restarted.append(True)).run_once()
    assert restarted == [True]
    assert row("a.txt")["state"] == "failed" and "Vaqt tugadi" in row("a.txt")["error"]
    assert version("a.txt")["attempts"] == 1  # not retried: it would only time out again


def test_a_worker_that_lost_its_claim_cannot_overwrite_the_new_owner(tmp_path, monkeypatch, verified):
    queue(tmp_path)

    def being_replaced(*args, **kwargs):
        with db.pool().connection() as conn:  # this worker stalls; another one takes the version over
            conn.execute("UPDATE document_versions SET heartbeat_at = now() - interval '10 minutes'")
        db.requeue_stale(90)
        assert db.claim_next("other-worker")["claimed_by"] == "other-worker"
        time.sleep(0.2)
        return 4

    monkeypatch.setattr(ingest, "index_version", being_replaced)
    make_worker().run_once()
    found = version("a.txt")
    assert found["status"] == "PROCESSING" and found["claimed_by"] == "other-worker"  # the late result was discarded
    assert row("a.txt")["version"] is None


# ---------- versions ----------

def test_a_version_walks_through_every_stage_and_is_active_only_at_the_end(tmp_path, monkeypatch, verified):
    queue(tmp_path)
    stages = []

    def watching(client, converter, chunker, path, *, document_id, version_id, file_name, file_hash, stage):
        stages.append(version("a.txt")["status"])
        stage("EMBEDDING")
        stages.append(version("a.txt")["status"])
        stage("INDEXING")
        stages.append(version("a.txt")["status"])
        return 3

    monkeypatch.setattr(versioning, "verify", lambda *args: stages.append(version("a.txt")["status"]))
    monkeypatch.setattr(ingest, "index_version", watching)
    make_worker().run_once()
    assert stages == ["PROCESSING", "EMBEDDING", "INDEXING", "VERIFYING"]
    assert version("a.txt")["status"] == "READY" and row("a.txt")["version"] == 1


def test_the_new_version_is_indexed_while_the_old_one_stays_active_and_then_replaces_it(tmp_path, monkeypatch,
                                                                                         verified):
    monkeypatch.setattr(ingest, "index_version", lambda *args, **kwargs: 2)
    queue(tmp_path, content=b"one")
    make_worker().run_once()
    old_id = str(version("a.txt")["id"])
    queue(tmp_path, content=b"two")
    seen_while_indexing = []

    def indexing(*args, **kwargs):
        seen_while_indexing.append(db.active_version_ids())  # what a search would see right now
        return 3

    monkeypatch.setattr(ingest, "index_version", indexing)
    make_worker().run_once()
    assert seen_while_indexing == [[old_id]]  # version 1 kept serving the whole time
    assert db.active_version_ids() == [str(version("a.txt", 2)["id"])]
    assert version("a.txt", 1)["status"] == "SUPERSEDED" and version("a.txt", 2)["status"] == "READY"
    assert row("a.txt")["chunks"] == 3 and row("a.txt")["version"] == 2


def test_a_failed_new_version_leaves_the_old_one_active_and_untouched(tmp_path, monkeypatch, verified):
    monkeypatch.setattr(config, "JOB_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(ingest, "index_version", lambda *args, **kwargs: 2)
    queue(tmp_path, content=b"one")
    make_worker().run_once()
    old = version("a.txt")
    queue(tmp_path, content=b"two")

    def qdrant_down(*args, **kwargs):
        raise ConnectionError("qdrant is down")

    monkeypatch.setattr(ingest, "index_version", qdrant_down)
    worker = make_worker()
    worker.run_once()
    worker.run_once()
    assert version("a.txt", 2)["status"] == "FAILED"
    assert db.active_version_ids() == [str(old["id"])]
    assert version("a.txt", 1)["status"] == "READY" and version("a.txt", 1)["activated_at"] == old["activated_at"]
    found = row("a.txt")
    assert found["state"] == "done" and found["version"] == 1 and found["chunks"] == 2  # still fully usable
    assert "qdrant is down" in found["update_error"] and found["error"] is None


def test_a_stored_file_that_is_not_the_uploaded_one_is_never_indexed(tmp_path, indexed):
    queued = queue(tmp_path, content=b"asl matn")
    storage.resolve(queued["file_path"]).write_bytes(b"boshqa matn")  # changed on disk after the upload
    make_worker().run_once()
    assert indexed == []  # nothing was parsed
    assert row("a.txt")["state"] == "failed" and "hash" in row("a.txt")["error"]
    assert version("a.txt")["attempts"] == 1  # retrying cannot help, so it is not retried


def test_the_verified_chunk_count_is_recorded_before_activation(tmp_path, monkeypatch, verified):
    monkeypatch.setattr(ingest, "index_version", lambda *args, **kwargs: 6)
    seen = []
    monkeypatch.setattr(versioning, "verify", lambda *args: seen.append(version("a.txt")["chunk_count"]))
    queue(tmp_path)
    make_worker().run_once()
    assert seen == [None]  # still unverified while the check runs
    assert version("a.txt")["chunk_count"] == 6 and version("a.txt")["status"] == "READY"


def test_an_incomplete_index_is_never_activated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOB_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(ingest, "index_version", lambda *args, **kwargs: 5)

    def incomplete(*args):
        raise versioning.IndexIncomplete("Qdrant da 3 ta bo'lak bor, 5 ta kerak")

    monkeypatch.setattr(versioning, "verify", incomplete)
    queue(tmp_path)
    make_worker().run_once()
    assert version("a.txt")["status"] == "FAILED" and "3 ta" in row("a.txt")["error"]
    assert db.active_version_ids() == [] and row("a.txt")["version"] is None


def test_a_worker_whose_claim_is_lost_at_the_last_moment_cannot_activate(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "index_version", lambda *args, **kwargs: 4)

    def verified_then_replaced(*args):
        with db.pool().connection() as conn:  # the claim changes hands between verifying and activating
            conn.execute("UPDATE document_versions SET claimed_by = 'someone-else'")

    monkeypatch.setattr(versioning, "verify", verified_then_replaced)
    queue(tmp_path)
    make_worker().run_once()
    assert version("a.txt")["status"] == "VERIFYING" and row("a.txt")["version"] is None
    assert db.active_version_ids() == []


def test_a_crash_in_the_middle_of_the_next_version_does_not_touch_the_active_one(tmp_path, monkeypatch, verified):
    monkeypatch.setattr(ingest, "index_version", lambda *args, **kwargs: 2)
    queue(tmp_path, content=b"one")
    make_worker().run_once()
    queue(tmp_path, content=b"two")
    dead = db.claim_next("dead-worker")
    assert dead["version_number"] == 2 and db.set_stage(dead["id"], "dead-worker", "EMBEDDING")  # then it died
    age = "UPDATE document_versions SET heartbeat_at = now() - interval '10 minutes' WHERE id = %s"
    with db.pool().connection() as conn:
        conn.execute(age, (dead["id"],))
    assert db.requeue_stale(90) == 1
    assert version("a.txt", 2)["status"] == "QUEUED" and version("a.txt", 1)["status"] == "READY"
    assert db.active_version_ids() == [str(version("a.txt", 1)["id"])]
    assert row("a.txt")["state"] == "queued" and row("a.txt")["version"] == 1  # searchable all along
