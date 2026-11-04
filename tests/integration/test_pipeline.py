"""Runs against the real Qdrant, Ollama, Tesseract and Docling. Enable with RAG_INTEGRATION=1.

The last test also needs PostgreSQL (RAG_TEST_DATABASE_URL) because it runs the real worker behind the real API.
"""
import os
import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from rag import answer, config, db, ingest, llm, storage, store, worker
from rag.api import create_app
from tests.fixtures.make_samples import CASES, UNANSWERABLE, make_bad_files, make_pdf, make_samples

pytestmark = pytest.mark.integration


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
    top = answer.retrieve(question, index[0])[0].payload
    assert top["file_name"] == file_name
    assert fact in top["text"]


@pytest.mark.parametrize("question,file_name,fact", CASES, ids=[c[1] for c in CASES])
def test_the_answer_contains_the_fact(index, question, file_name, fact):
    hits = answer.retrieve(question, index[0])
    text = "".join(answer.stream_answer(question, hits))
    assert fact.lower() in text.lower()
    assert answer.NOT_FOUND not in text


@pytest.mark.parametrize("question", UNANSWERABLE)
def test_questions_the_documents_cannot_answer_get_not_found(index, question):
    hits = answer.retrieve(question, index[0])
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
    texts = [h.payload["text"] for h in answer.retrieve("Muddat qachon?", client)]
    assert any("23-aprel" in t for t in texts) and not any("17-mart" in t for t in texts)
    assert store.count_chunks(client, str(path)) == 1


def test_the_index_is_keyed_by_the_given_source_not_the_path(scratch):
    client, docs, (converter, chunker) = scratch
    path = docs / "a.md"
    path.write_text("# Reja\n\nMuddat 17-mart.\n", encoding="utf-8")
    assert ingest.index_file(client, converter, chunker, path, source="doc-1") == 1
    assert store.indexed_sources(client) == {"doc-1": 1}
    assert [h.payload["file_name"] for h in answer.retrieve("Muddat qachon?", client)] == ["a.md"]


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
        conn.execute("TRUNCATE documents")
    client, _, (converter, chunker) = scratch
    big = make_pdf(tmp_path / "big.pdf", pages=60, dense=True)

    with TestClient(create_app(), base_url=f"http://127.0.0.1:{config.PORT}") as web:
        upload = web.post("/api/documents", files=[("files", ("big.pdf", big.read_bytes()))])
        assert upload.json()["results"][0]["accepted"]
        doc_id = db.get_document("big.pdf")["id"]
        real_worker = worker.Worker()
        real_worker.client, real_worker.converter, real_worker.chunker = client, converter, chunker
        thread = threading.Thread(target=real_worker.run_once)
        thread.start()

        deadline = time.time() + 180
        while db.get_document("big.pdf")["state"] != "processing":
            assert time.time() < deadline, "the worker never started the file"
            time.sleep(0.2)
        assert web.delete("/api/documents/big.pdf").status_code == 409  # refused while it is being read
        assert storage.path_for(doc_id, "big.pdf").exists()

        thread.join(timeout=600)
        assert db.get_document("big.pdf")["state"] == "done"
        assert store.count_chunks(client, str(doc_id)) >= 1
        assert web.delete("/api/documents/big.pdf").status_code == 200
        assert not storage.path_for(doc_id, "big.pdf").exists()
        assert store.count_chunks(client, str(doc_id)) == 0
