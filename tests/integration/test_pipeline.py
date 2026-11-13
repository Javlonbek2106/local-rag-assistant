"""Runs against the real Qdrant, Ollama, Tesseract and Docling. Enable with RAG_INTEGRATION=1.

The last test also needs PostgreSQL (RAG_TEST_DATABASE_URL) because it runs the real worker behind the real API.
"""
import os
import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from rag import answer, config, db, ingest, llm, storage, store, versioning, worker
from rag.api import create_app
from tests.fixtures.make_samples import CASES, UNANSWERABLE, make_bad_files, make_pdf, make_samples

pytestmark = pytest.mark.integration


class NoVersions:
    """Indexes built with ingest.index_file hold plain chunks without versions: search them unfiltered."""

    @staticmethod
    def active_version_ids():
        return answer.UNVERSIONED


def _require_services():
    try:
        store.get_client().get_collections()
        llm.embed(["probe"])
    except Exception as error:
        pytest.skip(f"Qdrant/Ollama not reachable: {error}")


def _supported(folder):
    return sorted(p for p in folder.iterdir() if ingest.is_supported(p))


@pytest.fixture(scope="session")
def session_env():
    os.environ.setdefault("HF_HUB_OFFLINE", "1")  # models are cached; nothing should be downloaded in tests
    _require_services()
    mp = pytest.MonkeyPatch()
    collection = f"rag_it_{uuid.uuid4().hex[:8]}"
    mp.setattr(config, "COLLECTION", collection)
    yield collection
    store.get_client().delete_collection(collection)
    mp.undo()


@pytest.fixture(scope="session")
def pipeline():
    return ingest.build_pipeline()


@pytest.fixture(scope="session")
def index(session_env, pipeline, tmp_path_factory):
    """All sample documents indexed once and shared by the read-only tests."""
    incoming = make_samples(tmp_path_factory.mktemp("incoming"))
    client = store.get_client()
    store.ensure_collection(client, lambda: len(llm.embed(["probe"])[0]))
    converter, chunker = pipeline
    files = _supported(incoming)
    for path in files:
        assert ingest.index_file(client, converter, chunker, path) >= 1, path.name
    return client, files


@pytest.fixture
def scratch(monkeypatch, tmp_path, pipeline):
    """A private collection and folder for tests that change things."""
    _require_services()
    collection = f"rag_it_{uuid.uuid4().hex[:8]}"
    docs = tmp_path / "documents"
    docs.mkdir()
    monkeypatch.setattr(config, "COLLECTION", collection)
    client = store.get_client()
    store.ensure_collection(client, lambda: len(llm.embed(["probe"])[0]))
    yield client, docs, pipeline
    client.delete_collection(collection)


# ---------- reading and retrieval ----------

@pytest.mark.parametrize("question,file_name,fact", CASES, ids=[c[1] for c in CASES])
def test_the_right_file_is_the_top_hit_and_holds_the_fact(index, question, file_name, fact):
    top = answer.retrieve(question, index[0], NoVersions)[0].payload
    assert top["file_name"] == file_name
    assert fact in top["text"]


@pytest.mark.parametrize("question,file_name,fact", CASES, ids=[c[1] for c in CASES])
def test_the_answer_contains_the_fact(index, question, file_name, fact):
    hits = answer.retrieve(question, index[0], NoVersions)
    text = "".join(answer.stream_answer(question, hits))
    assert fact.lower() in text.lower()
    assert answer.NOT_FOUND not in text


@pytest.mark.parametrize("question", UNANSWERABLE)
def test_questions_the_documents_cannot_answer_get_not_found(index, question):
    hits = answer.retrieve(question, index[0], NoVersions)
    assert answer.NOT_FOUND in "".join(answer.stream_answer(question, hits))


def test_collection_has_payload_indexes_and_every_file_is_listed(index):
    client, files = index
    schema = client.get_collection(config.COLLECTION).payload_schema
    assert set(store.INDEXED_FIELDS) <= set(schema)
    assert set(store.indexed_sources(client)) == {str(p) for p in files}
    assert len(files) == len(CASES)


# ---------- re-indexing behaviour ----------

def test_unchanged_files_are_skipped_and_changed_files_replaced(scratch):
    client, docs, (converter, chunker) = scratch
    path = docs / "a.md"
    path.write_text("# Reja\n\nMuddat 17-mart.\n", encoding="utf-8")
    assert ingest.index_file(client, converter, chunker, path) == 1
    assert ingest.index_file(client, converter, chunker, path) is None  # same content: nothing to do

    path.write_text("# Reja\n\nMuddat 23-aprel.\n", encoding="utf-8")
    assert ingest.index_file(client, converter, chunker, path) == 1
    texts = [h.payload["text"] for h in answer.retrieve("Muddat qachon?", client, NoVersions)]
    assert any("23-aprel" in t for t in texts) and not any("17-mart" in t for t in texts)
    assert store.count_chunks(client, str(path)) == 1


def test_the_index_is_keyed_by_the_given_source_not_the_path(scratch):
    client, docs, (converter, chunker) = scratch
    path = docs / "a.md"
    path.write_text("# Reja\n\nMuddat 17-mart.\n", encoding="utf-8")
    assert ingest.index_file(client, converter, chunker, path, source="doc-1") == 1
    assert store.indexed_sources(client) == {"doc-1": 1}
    assert [h.payload["file_name"] for h in answer.retrieve("Muddat qachon?", client, NoVersions)] == ["a.md"]


def test_a_file_that_becomes_unreadable_keeps_its_old_chunks(scratch):
    client, docs, (converter, chunker) = scratch
    path = docs / "a.pdf"
    make_pdf(path, pages=2, fact="Zaxira generatorining sig'imi 850 litr.", fact_page=1)
    assert ingest.index_file(client, converter, chunker, path) >= 1
    before = store.count_chunks(client, str(path))
    path.write_bytes(b"broken")
    with pytest.raises(Exception):  # noqa: B017 - the exact Docling error type is not part of our contract
        ingest.index_file(client, converter, chunker, path)
    assert store.count_chunks(client, str(path)) == before


def test_bad_files_fail_cleanly_and_never_reach_the_index(scratch, tmp_path):
    client, docs, (converter, chunker) = scratch
    bad = make_bad_files(tmp_path / "bad")
    assert [p.name for p in bad.iterdir() if not ingest.is_supported(p)] == ["21_eski.doc", "~$03_vaqtinchalik.docx"]
    for path in _supported(bad):
        with pytest.raises(Exception):  # noqa: B017
            ingest.index_file(client, converter, chunker, path)
    assert store.indexed_sources(client) == {}


def test_orphaned_index_entries_are_purged_but_known_documents_are_kept(scratch):
    client, docs, (converter, chunker) = scratch
    for name in ("keep", "gone"):
        path = docs / f"{name}.md"
        path.write_text(f"# {name}\n\ntext of {name}\n", encoding="utf-8")
        ingest.index_file(client, converter, chunker, path, source=f"id-{name}")
    assert worker.purge_orphans(client, {"id-keep"}) == 1
    assert set(store.indexed_sources(client)) == {"id-keep"}


# ---------- the real worker behind the real API ----------

def test_deleting_a_file_that_is_being_read_is_refused_then_works(scratch, storage_dir, request, tmp_path):
    if not os.getenv("RAG_TEST_DATABASE_URL"):
        pytest.skip("needs PostgreSQL: set RAG_TEST_DATABASE_URL")
    request.getfixturevalue("database")
    with db.pool().connection() as conn:
        conn.execute("TRUNCATE documents, document_versions")
    client, _, (converter, chunker) = scratch
    big = make_pdf(tmp_path / "big.pdf", pages=60, dense=True)

    with TestClient(create_app(), base_url=f"http://127.0.0.1:{config.PORT}") as web:
        upload = web.post("/api/documents", files=[("files", ("big.pdf", big.read_bytes()))])
        assert upload.json()["results"][0]["accepted"]
        doc_id = db.get_document("big.pdf")["id"]
        stored = storage.resolve(db.get_version("big.pdf", 1)["file_path"])
        real_worker = worker.Worker()
        real_worker.client, real_worker.converter, real_worker.chunker = client, converter, chunker
        thread = threading.Thread(target=real_worker.run_once)
        thread.start()

        deadline = time.time() + 180
        while db.get_document("big.pdf")["state"] != "processing":
            assert time.time() < deadline, "the worker never started the file"
            time.sleep(0.2)
        assert web.delete("/api/documents/big.pdf").status_code == 409  # refused while it is being read
        assert stored.exists()

        thread.join(timeout=600)
        assert db.get_document("big.pdf")["state"] == "done"
        assert store.count_chunks(client, str(doc_id)) >= 1
        assert web.delete("/api/documents/big.pdf").status_code == 200
        assert not stored.exists()
        assert store.count_chunks(client, str(doc_id)) == 0


# ---------- versions on the real Qdrant ----------

@pytest.fixture
def tiny(monkeypatch):
    """A private collection of 3-dimensional vectors: the filters are Qdrant's job, no embedding model is needed."""
    from qdrant_client import models

    try:
        client = store.get_client()
        client.get_collections()
    except Exception as error:
        pytest.skip(f"Qdrant not reachable: {error}")
    collection = f"rag_it_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(config, "COLLECTION", collection)
    client.create_collection(collection, vectors_config=models.VectorParams(size=3, distance=models.Distance.COSINE))
    store.ensure_collection(client, lambda: 3)
    yield client
    client.delete_collection(collection)


def put(client, document_id, version_id, texts, file_hash="h", legacy=False):
    """Index chunks the way ingest.index_version does (or, with legacy=True, the way it was done before versions)."""
    items = [(text, [1], []) for text in texts]
    vectors = [[1.0, 0.0, float(i)] for i in range(len(texts))]
    points = ingest._points(items, vectors, str(document_id), "a.txt", file_hash,
                            None if legacy else document_id, None if legacy else version_id)
    client.upload_points(config.COLLECTION, points=points, wait=True)
    return points


def test_search_returns_only_chunks_of_the_active_versions(tiny):
    doc_a, doc_b, v1, v2, vb = (uuid.uuid4() for _ in range(5))
    put(tiny, doc_a, v1, ["a eski 1", "a eski 2"])
    put(tiny, doc_a, v2, ["a yangi"])
    put(tiny, doc_b, vb, ["b matn"])
    hits = tiny.query_points(config.COLLECTION, query=[1.0, 0.0, 0.0], limit=10, with_payload=True,
                             query_filter=store.active_filter([str(v1), str(vb)])).points
    assert sorted(h.payload["text"] for h in hits) == ["a eski 1", "a eski 2", "b matn"]
    hits = tiny.query_points(config.COLLECTION, query=[1.0, 0.0, 0.0], limit=10, with_payload=True,
                             query_filter=store.active_filter([str(v2)])).points
    assert [h.payload["text"] for h in hits] == ["a yangi"]  # never a mix of the two versions of one document


def test_a_version_has_its_own_points_and_the_same_version_written_twice_adds_no_duplicates(tiny):
    doc, v1, v2 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    first = put(tiny, doc, v1, ["bir", "ikki", "uch"])
    put(tiny, doc, v1, ["bir", "ikki", "uch"])  # a retry writes the same version again
    put(tiny, doc, v2, ["bir", "ikki", "uch"])  # the same text in another version is a separate set of points
    assert store.count_version(tiny, v1) == 3 and store.count_version(tiny, v2) == 3
    assert {p.id for p in first}.isdisjoint({p.id for p in put(tiny, doc, v2, ["bir"])})


def test_a_retry_that_produces_fewer_chunks_leaves_no_leftovers(tiny):
    doc, version = uuid.uuid4(), uuid.uuid4()
    put(tiny, doc, version, ["1", "2", "3", "4"])  # the first attempt got further than the second will
    store.delete_version(tiny, version)  # what index_version does before it uploads
    put(tiny, doc, version, ["1", "2"])
    assert store.count_version(tiny, version) == 2


def test_deleting_one_version_leaves_the_others(tiny):
    doc, v1, v2 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    put(tiny, doc, v1, ["a", "b"])
    put(tiny, doc, v2, ["c"])
    store.delete_version(tiny, v1)
    assert store.count_version(tiny, v1) == 0 and store.count_version(tiny, v2) == 1


def test_verification_accepts_a_complete_index_and_rejects_every_kind_of_damage(tiny):
    doc, other, version = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    put(tiny, doc, version, ["1", "2", "3"], file_hash="abc")
    versioning.verify(tiny, doc, version, "abc", 3)  # complete: no error
    with pytest.raises(versioning.IndexIncomplete):
        versioning.verify(tiny, doc, version, "abc", 4)  # a chunk is missing
    with pytest.raises(versioning.IndexIncomplete):
        versioning.verify(tiny, doc, version, "abc", 2)  # more chunks than expected (a leftover)
    with pytest.raises(versioning.IndexIncomplete):
        versioning.verify(tiny, other, version, "abc", 3)  # the chunks belong to another document
    with pytest.raises(versioning.IndexIncomplete):
        versioning.verify(tiny, doc, version, "different", 3)  # they were made from another file
    versioning.verify(tiny, doc, version, None, 3)  # hash unknown (a migrated version): the rest still holds


def test_legacy_chunks_are_tagged_once_and_never_overwrite_versioned_ones(tiny):
    doc, tagged = uuid.uuid4(), uuid.uuid4()
    put(tiny, doc, None, ["eski 1", "eski 2"], legacy=True)  # indexed before versioning: source only
    put(tiny, doc, tagged, ["yangi"])  # a later version of the same document
    migrated = uuid.uuid4()
    store.tag_legacy(tiny, doc, migrated)
    store.tag_legacy(tiny, doc, uuid.uuid4())  # repeating, even with another id, changes nothing
    assert store.count_version(tiny, migrated) == 2  # the two old chunks, now version 1
    assert store.count_version(tiny, tagged) == 1  # the versioned chunk kept its own id
    versioning.verify(tiny, doc, migrated, None, 2)


def test_a_missing_vector_makes_a_rollback_refuse_on_the_real_index(tiny, monkeypatch):
    doc, v1 = uuid.uuid4(), uuid.uuid4()
    points = put(tiny, doc, v1, ["1", "2", "3"], file_hash="abc")
    from qdrant_client import models

    tiny.delete(config.COLLECTION, points_selector=models.PointIdsList(points=[points[1].id]))

    class Repo:
        def get_version(self, name, number):
            return {"id": v1, "document_id": doc, "file_hash": "abc", "chunk_count": 3, "status": "SUPERSEDED",
                    "active": False}

        def switch_active(self, version_id):
            raise AssertionError("must not switch")

    with pytest.raises(versioning.RollbackError, match="to'liq emas"):
        versioning.rollback(Repo(), tiny, "a.txt", 1)


def test_two_versions_of_a_document_with_the_real_worker_and_rollback(scratch, storage_dir, request):
    """The acceptance test: version 1 stays searchable while version 2 is indexed; only a verified version
    replaces it; rolling back needs no re-embedding."""
    if not os.getenv("RAG_TEST_DATABASE_URL"):
        pytest.skip("needs PostgreSQL: set RAG_TEST_DATABASE_URL")
    request.getfixturevalue("database")
    with db.pool().connection() as conn:
        conn.execute("TRUNCATE documents, document_versions")
    client, _, (converter, chunker) = scratch
    real_worker = worker.Worker()
    real_worker.client, real_worker.converter, real_worker.chunker = client, converter, chunker

    def texts():
        return [h.payload["text"] for h in answer.search("Maxfiy parol qanday?", client, 5, db.active_version_ids())]

    with TestClient(create_app(), base_url=f"http://127.0.0.1:{config.PORT}") as web:
        def put_file(content):
            assert web.post("/api/documents", files=[("files", ("parol.txt", content.encode()))]).status_code == 200

        put_file("Maxfiy parol: ALFA-111.")
        assert real_worker.run_once() and texts() and all("ALFA-111" in t for t in texts())

        put_file("Maxfiy parol: BETA-222.")
        assert texts() and all("ALFA-111" in t for t in texts())  # version 2 is queued: version 1 still answers
        assert real_worker.run_once()
        assert texts() and all("BETA-222" in t for t in texts())  # verified and active: version 2 answers
        assert db.get_document("parol.txt")["version"] == 2

        before = client.count(config.COLLECTION, exact=True).count  # both versions' chunks are in the index
        assert before >= 2
        assert web.post("/api/documents/parol.txt/rollback", json={"version": 1}).status_code == 200
        assert client.count(config.COLLECTION, exact=True).count == before  # nothing was embedded or deleted
        assert texts() and all("ALFA-111" in t for t in texts())
