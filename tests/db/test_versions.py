"""Document versions in a real PostgreSQL: numbering, activation, rollback and the migration of existing documents."""
import hashlib
import threading
import uuid

import psycopg
import pytest

from rag import answer, config, db, storage, versioning
from tests.conftest import FakeQdrant

pytestmark = pytest.mark.db


def upload(name="a.pdf", content=None, size=10):
    """A new queued version of `name`; the content decides the hash (equal content is the same file)."""
    version = db.create_version(name, size, hashlib.sha256((content or uuid.uuid4().hex).encode()).hexdigest())
    if version["created"]:
        assert db.enqueue(version["id"])
    return version


def activate(job, worker="w1", chunks=3):
    assert db.set_stage(job["id"], worker, "VERIFYING")
    return db.activate(job["id"], worker, chunks)


def publish(name="a.pdf", content=None, chunks=3):
    """Upload, claim and activate: a version that is live."""
    created = upload(name, content)
    job = db.claim_next("w1")
    assert job["id"] == created["id"]
    assert activate(job, chunks=chunks) == "activated"
    return job


def statuses(name):
    return [(v["version_number"], v["status"], v["active"]) for v in reversed(db.list_versions(name))]


# ---------- numbering ----------

def test_the_first_upload_is_version_1_and_the_next_is_version_2():
    first, second = upload(content="one"), upload(content="two")
    assert (first["version_number"], second["version_number"]) == (1, 2)
    assert first["document_id"] == second["document_id"]
    assert first["id"] != second["id"] and first["file_path"] != second["file_path"]


def test_concurrent_uploads_of_one_name_get_distinct_consecutive_version_numbers():
    numbers, errors, lock = [], [], threading.Lock()

    def worker(i):
        try:
            created = upload(content=f"content {i}")
            with lock:
                numbers.append(created["version_number"])
        except Exception as error:  # noqa: BLE001 - every failure must reach the assertion
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and sorted(numbers) == list(range(1, 13))
    assert len(db.list_documents()) == 1  # one document, twelve versions


def test_concurrent_first_uploads_of_a_new_name_create_one_document():
    errors = []

    def worker(i):
        try:
            upload("new.pdf", content=f"c{i}")
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(db.list_documents()) == 1 and len(db.list_versions("new.pdf")) == 6


def test_an_identical_file_is_not_a_new_version_unless_the_newest_one_failed():
    first = upload(content="same")
    again = upload(content="same")
    assert again["created"] is False and again["id"] == first["id"] and again["version_number"] == 1
    job = db.claim_next("w1")
    db.fail(job["id"], "w1", "unreadable", retry=False)
    retry = upload(content="same")  # the user fixes the problem elsewhere and uploads the same file again
    assert retry["created"] is True and retry["version_number"] == 2


def test_different_documents_never_share_versions_or_files():
    a, b = upload("a.pdf", "same bytes"), upload("b.pdf", "same bytes")
    assert a["document_id"] != b["document_id"] and a["id"] != b["id"] and a["file_path"] != b["file_path"]
    assert str(a["document_id"]) in a["file_path"] and str(b["document_id"]) in b["file_path"]
    assert (a["version_number"], b["version_number"]) == (1, 1)  # numbers are per document


# ---------- the active version ----------

def test_the_active_version_must_belong_to_the_same_document():
    publish("a.pdf")
    other = upload("b.pdf")
    with pytest.raises(psycopg.errors.ForeignKeyViolation), db.pool().connection() as conn:
        conn.execute("UPDATE documents SET active_version_id = %s WHERE name = 'a.pdf'", (other["id"],))


def test_only_a_verified_version_can_be_activated():
    upload()
    job = db.claim_next("w1")
    assert db.activate(job["id"], "w1", 3) is None  # still PROCESSING: nothing was verified
    assert db.active_version_ids() == []
    assert db.set_stage(job["id"], "w1", "VERIFYING") and db.activate(job["id"], "w1", 3) == "activated"


def test_activation_replaces_the_previous_version_in_one_step():
    first = publish(content="one", chunks=2)
    assert db.active_version_ids() == [str(first["id"])]
    publish(content="two", chunks=5)
    assert statuses("a.pdf") == [(1, "SUPERSEDED", False), (2, "READY", True)]
    found = db.get_document("a.pdf")
    assert found["version"] == 2 and found["chunks"] == 5 and found["state"] == "done"
    assert len(db.active_version_ids()) == 1
    assert db.get_version("a.pdf", 1)["chunk_count"] == 2  # the old version keeps its data for rollback


def test_an_older_version_finishing_late_never_replaces_a_newer_active_one():
    upload(content="one")
    v1 = db.claim_next("w1")
    v2 = upload(content="two")
    v3 = upload(content="three")
    # v2 and v3 were processed in parallel after v1's worker died; v3 finishes first
    with db.pool().connection() as conn:
        conn.execute("UPDATE document_versions SET status = 'VERIFYING', claimed_by = 'w3' WHERE id = %s",
                     (v3["id"],))
        conn.execute("UPDATE document_versions SET status = 'VERIFYING', claimed_by = 'w2' WHERE id = %s",
                     (v2["id"],))
        conn.execute("UPDATE document_versions SET status = 'FAILED' WHERE id = %s", (v1["id"],))
    assert db.activate(v3["id"], "w3", 3) == "activated"
    assert db.activate(v2["id"], "w2", 2) == "superseded"  # too late: kept for rollback, not made active
    assert db.active_version_ids() == [str(v3["id"])]
    assert db.get_version("a.pdf", 2)["status"] == "SUPERSEDED" and db.get_version("a.pdf", 2)["chunk_count"] == 2


def test_a_document_has_one_version_in_flight_at_a_time_and_versions_run_in_order():
    first, second = upload(content="1"), upload(content="2")
    upload(content="3")
    job = db.claim_next("w1")
    assert job["id"] == first["id"]
    assert db.claim_next("w2") is None  # versions 2 and 3 wait for version 1
    activate(job)
    assert db.claim_next("w2")["id"] == second["id"]
    assert db.claim_next("w3") is None  # version 3 waits for version 2


def test_many_workers_polling_one_documents_versions_never_overlap():
    for i in range(5):
        upload(content=f"c{i}")
    claimed, lock = [], threading.Lock()

    def poll(worker):
        job = db.claim_next(worker)
        if job:
            with lock:
                claimed.append(job["version_number"])

    threads = [threading.Thread(target=poll, args=(f"w{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert claimed == [1]  # exactly one version of the document is taken


def test_documents_do_not_block_each_other():
    upload("a.pdf")
    upload("b.pdf")
    assert {db.claim_next("w1")["name"], db.claim_next("w2")["name"]} == {"a.pdf", "b.pdf"}


# ---------- failures ----------

def test_a_failed_version_never_becomes_active_and_the_old_one_keeps_serving():
    old = publish(content="one", chunks=4)
    upload(content="two")
    job = db.claim_next("w1")
    assert db.fail(job["id"], "w1", "Ollama to'xtadi", retry=False) == "failed"
    assert db.active_version_ids() == [str(old["id"])]
    found = db.get_document("a.pdf")
    assert (found["state"], found["version"], found["chunks"]) == ("done", 1, 4)
    assert found["update_error"] == "Ollama to'xtadi" and found["error"] is None
    assert statuses("a.pdf") == [(1, "READY", True), (2, "FAILED", False)]


def test_a_version_that_keeps_failing_is_retried_by_the_policy_then_given_up(monkeypatch):
    monkeypatch.setattr(config, "JOB_MAX_ATTEMPTS", 2)
    publish(content="one")
    upload(content="two")
    assert db.fail(db.claim_next("w1")["id"], "w1", "boom") == "queued"
    assert db.fail(db.claim_next("w1")["id"], "w1", "boom") == "failed"
    assert db.claim_next("w1") is None and db.get_document("a.pdf")["version"] == 1


def test_a_failed_first_version_leaves_a_failed_document_that_nothing_can_find():
    upload()
    db.fail(db.claim_next("w1")["id"], "w1", "no text", retry=False)
    found = db.get_document("a.pdf")
    assert found["state"] == "failed" and found["error"] == "no text" and found["version"] is None
    assert db.active_version_ids() == []


# ---------- rollback ----------

def test_switch_active_returns_to_a_superseded_version_and_back():
    first = publish(content="one", chunks=2)
    second = publish(content="two", chunks=5)
    db.switch_active(first["id"])
    assert statuses("a.pdf") == [(1, "READY", True), (2, "SUPERSEDED", False)]
    assert db.active_version_ids() == [str(first["id"])]
    found = db.get_document("a.pdf")
    assert found["version"] == 1 and found["chunks"] == 2 and found["state"] == "done"
    db.switch_active(second["id"])
    assert db.active_version_ids() == [str(second["id"])]


@pytest.mark.parametrize("prepare, expect", [
    ("active", "READY"),
    ("failed", "FAILED"),
    ("queued", "QUEUED"),
])
def test_switch_active_refuses_a_version_that_was_never_a_valid_fallback(prepare, expect):
    publish(content="one")
    upload(content="two")
    target = db.get_version("a.pdf", 1 if prepare == "active" else 2)
    if prepare == "failed":
        db.fail(db.claim_next("w1")["id"], "w1", "x", retry=False)
    with pytest.raises(db.VersionConflict, match=expect):
        db.switch_active(target["id"])
    assert db.active_version_ids() == [str(db.get_version("a.pdf", 1)["id"])]  # nothing changed


def test_deleting_a_document_removes_all_its_versions_even_the_active_one():
    publish(content="one")
    publish(content="two")
    publish("b.pdf")
    assert db.delete_document("a.pdf") is not None
    with db.pool().connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM document_versions").fetchone()["n"] == 1
    assert [d["name"] for d in db.list_documents()] == ["b.pdf"]


def test_a_document_with_a_version_in_flight_cannot_be_deleted():
    publish(content="one")
    upload(content="two")
    db.claim_next("w1")
    with pytest.raises(db.Busy):
        db.delete_document("a.pdf")
    assert len(db.list_versions("a.pdf")) == 2


# ---------- documents that existed before versioning ----------

LEGACY_DOCUMENTS = """
INSERT INTO documents (name, size_bytes, state, chunks, error, attempts) VALUES
  ('done.pdf', 100, 'done', 7, NULL, 1),
  ('queued.pdf', 200, 'queued', NULL, NULL, 0),
  ('failed.pdf', 300, 'failed', NULL, 'no text', 1),
  ('busy.pdf', 400, 'processing', NULL, NULL, 1);
"""


@pytest.fixture
def legacy_database(monkeypatch):
    """A schema as it was before versioning (only migration 0001) holding four documents, migrated now."""
    schema = f"rag_legacy_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    monkeypatch.setattr(config, "DB_SCHEMA", schema)
    db.pool.cache_clear()
    try:
        with db.pool().connection() as conn:
            conn.execute("CREATE TABLE schema_migrations (version text PRIMARY KEY, "
                         "applied_at timestamptz NOT NULL DEFAULT now())")
            conn.execute((db.MIGRATIONS / "0001_documents.sql").read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (version) VALUES ('0001_documents')")
            conn.execute(LEGACY_DOCUMENTS)
        db.migrate()
        yield schema
    finally:
        db.close()
        with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")
        db.pool.cache_clear()


def test_existing_documents_become_version_1_with_their_state_and_the_finished_one_is_active(legacy_database):
    found = {d["name"]: d for d in db.list_documents()}
    assert found["done.pdf"]["state"] == "done" and found["done.pdf"]["chunks"] == 7
    assert (found["done.pdf"]["version"], found["done.pdf"]["latest_version"]) == (1, 1)
    version = db.get_version("done.pdf", 1)
    assert version["status"] == "READY" and version["active"] and version["chunk_count"] == 7
    assert version["file_path"] == f"{found['done.pdf']['id']}/done.pdf"  # the file did not move
    assert version["file_hash"] is None  # not known until the backfill
    assert {n: db.get_version(n, 1)["status"] for n in ("queued.pdf", "failed.pdf", "busy.pdf")} == {
        "queued.pdf": "QUEUED", "failed.pdf": "FAILED", "busy.pdf": "PROCESSING"}
    assert found["failed.pdf"]["state"] == "failed" and found["failed.pdf"]["error"] == "no text"
    assert db.active_version_ids() == [str(version["id"])]  # only the finished document is searchable


def test_existing_work_in_the_queue_is_still_picked_up_after_the_migration(legacy_database):
    job = db.claim_next("w1")
    assert job["name"] == "queued.pdf" and job["version_number"] == 1 and job["attempts"] == 1


def test_a_re_upload_of_an_existing_document_is_version_2_and_keeps_serving_version_1(legacy_database):
    second = db.create_version("done.pdf", 150, "new-hash")
    assert second["version_number"] == 2 and second["created"]
    assert db.get_document("done.pdf")["version"] == 1


def test_the_backfill_tags_old_chunks_and_fills_in_the_file_hash(legacy_database, storage_dir):
    doc = db.get_document("done.pdf")
    version = db.get_version("done.pdf", 1)
    legacy_file = storage_dir / str(doc["id"]) / "done.pdf"
    legacy_file.parent.mkdir(parents=True)
    legacy_file.write_bytes(b"old bytes")
    qdrant = FakeQdrant()
    for i in range(7):  # how the chunks were written before versioning: a source and a hash, no version
        qdrant.points[f"old-{i}"] = ([0.1], {"source": str(doc["id"]), "file_hash": "x", "chunks_total": 7,
                                             "file_name": "done.pdf", "text": f"chunk {i}"})
    qdrant.points["other"] = ([0.1], {"source": "some-other-document", "text": "unrelated"})

    assert versioning.backfill_legacy(db, qdrant) == 4  # every migrated version is registered
    assert {p["version_id"] for _v, p in qdrant.points.values() if p["source"] == str(doc["id"])} == {
        str(version["id"])}
    assert "version_id" not in qdrant.points["other"][1]  # another document's chunk is untouched
    assert db.get_version("done.pdf", 1)["file_hash"] == hashlib.sha256(b"old bytes").hexdigest()
    assert db.get_version("queued.pdf", 1)["file_hash"] is None  # no stored file to hash

    assert versioning.backfill_legacy(db, qdrant) == 3  # repeating is harmless; what has a hash is skipped
    from unittest import mock

    with mock.patch("rag.answer.llm.embed", lambda texts: [[0.1]]):
        hits = answer.search("savol", qdrant, 20, db.active_version_ids())
    assert len(hits) == 7 and {h.payload["source"] for h in hits} == {str(doc["id"])}  # found through the new filter


def test_rollback_to_a_migrated_version_works_once_its_chunks_are_tagged(legacy_database, storage_dir):
    doc = db.get_document("done.pdf")
    qdrant = FakeQdrant()
    for i in range(7):
        qdrant.points[f"old-{i}"] = ([0.1], {"source": str(doc["id"]), "file_hash": "x", "chunks_total": 7})
    versioning.backfill_legacy(db, qdrant)
    new = db.create_version("done.pdf", 150, "new-hash")
    db.enqueue(new["id"])
    job = db.claim_next("w1")
    while job["name"] != "done.pdf":  # the legacy queue holds an older document too
        job = db.claim_next("w1")
    activate(job, chunks=2)  # version 2 goes live: version 1 becomes SUPERSEDED but keeps its chunks
    assert db.get_version("done.pdf", 1)["status"] == "SUPERSEDED"
    # the legacy chunks say file_hash "x" (not known to the database), which is why verification only demands the
    # hash when the database has one; here it has none yet because no stored file exists
    restored = versioning.rollback(db, qdrant, "done.pdf", 1)
    assert restored["active"] and db.get_document("done.pdf")["version"] == 1
    assert storage.resolve(restored["file_path"]).parent == storage_dir.resolve() / str(doc["id"])
