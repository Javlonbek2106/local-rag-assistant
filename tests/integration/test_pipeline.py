"""Runs against the real Qdrant, Ollama, Tesseract and Docling. Enable with RAG_INTEGRATION=1."""
import os
import subprocess
import sys
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from rag import answer, config, ingest, llm, store, worker
from rag.api import create_app
from tests.fixtures.make_samples import CASES, UNANSWERABLE, make_bad_files, make_pdf, make_samples

pytestmark = pytest.mark.integration


def _require_services():
    try:
        store.get_client().get_collections()
        llm.embed(["probe"])
    except Exception as error:
        pytest.skip(f"Qdrant/Ollama not reachable: {error}")


@pytest.fixture(scope="session")
def session_env(tmp_path_factory):
    os.environ.setdefault("HF_HUB_OFFLINE", "1")  # models are cached; nothing should be downloaded in tests
    _require_services()
    mp = pytest.MonkeyPatch()
    collection = f"rag_it_{uuid.uuid4().hex[:8]}"
    docs = tmp_path_factory.mktemp("documents")
    mp.setattr(config, "COLLECTION", collection)
    mp.setattr(config, "DOCS_DIR", docs)
    yield collection, docs
    store.get_client().delete_collection(collection)
    mp.undo()


@pytest.fixture(scope="session")
def pipeline():
    return ingest.build_pipeline()


@pytest.fixture(scope="session")
def index(session_env, pipeline, tmp_path_factory):
    """All sample documents indexed once and shared by the read-only tests."""
    _, docs = session_env
    incoming = tmp_path_factory.mktemp("incoming")
    make_samples(incoming)
    client = store.get_client()
    store.ensure_collection(client, lambda: len(llm.embed(["probe"])[0]))
    paths, _ = ingest.import_into_documents(incoming)
    converter, chunker = pipeline
    for path in paths:
        assert ingest.index_file(client, converter, chunker, path) >= 1, path.name
    return client, docs


@pytest.fixture
def scratch(monkeypatch, tmp_path, pipeline):
    """A private collection and documents folder for tests that change things."""
    _require_services()
    collection = f"rag_it_{uuid.uuid4().hex[:8]}"
    docs = tmp_path / "documents"
    docs.mkdir()
    monkeypatch.setattr(config, "COLLECTION", collection)
    monkeypatch.setattr(config, "DOCS_DIR", docs)
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
    client, docs = index
    schema = client.get_collection(config.COLLECTION).payload_schema
    assert set(store.INDEXED_FIELDS) <= set(schema)
    assert set(store.indexed_sources(client)) == {str(p) for p in worker.list_files()}
    assert len(worker.list_files()) == len(CASES)


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
    paths, problems = ingest.import_into_documents(bad)
    assert [p.name for p, _ in problems] == ["21_eski.doc"]  # lock file is not even listed as a problem
    for path in paths:
        with pytest.raises(Exception):  # noqa: B017
            ingest.index_file(client, converter, chunker, path)
    assert store.indexed_sources(client) == {}


def test_purge_removes_index_entries_of_files_deleted_outside_the_app(scratch):
    client, docs, (converter, chunker) = scratch
    keep, gone = docs / "keep.md", docs / "gone.md"
    keep.write_text("# A\n\nbir\n", encoding="utf-8")
    gone.write_text("# B\n\niki\n", encoding="utf-8")
    for path in (keep, gone):
        ingest.index_file(client, converter, chunker, path)
    gone.unlink()
    assert worker.purge_stale(client) == 1
    assert set(store.indexed_sources(client)) == {str(keep)}


def test_command_line_import_copies_into_documents_and_is_idempotent(scratch, tmp_path):
    client, docs, _ = scratch
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    (incoming / "a.md").write_text("# Reja\n\nMuddat 17-mart.\n", encoding="utf-8")
    env = {**os.environ, "RAG_COLLECTION": config.COLLECTION, "RAG_DOCS": str(docs), "PYTHONIOENCODING": "utf-8"}

    def run():
        return subprocess.run([sys.executable, "-m", "rag.ingest", str(incoming)], env=env,
                              capture_output=True, text=True, encoding="utf-8", cwd=config.ROOT)

    first, second = run(), run()
    assert first.returncode == 0 and "INDEXED   a.md" in first.stdout, first.stdout + first.stderr
    assert second.returncode == 0 and "UNCHANGED a.md" in second.stdout
    assert (docs / "a.md").exists()  # visible to the web app, unlike before
    assert set(store.indexed_sources(client)) == {str(docs / "a.md")}


# ---------- the real worker behind the real API ----------

def test_deleting_a_file_that_is_being_read_is_refused_then_works(scratch):
    client, docs, _ = scratch
    big = docs / "big.pdf"
    make_pdf(big, pages=60, dense=True)
    with TestClient(create_app(), base_url=f"http://127.0.0.1:{config.PORT}") as web:
        def state():
            rows = {r["name"]: r for r in web.get("/api/documents").json()["documents"]}
            return rows["big.pdf"]["state"]

        deadline = time.time() + 180
        while state() != "processing":
            assert time.time() < deadline, "the worker never started the file"
            time.sleep(0.5)
        assert web.delete("/api/documents/big.pdf").status_code == 409  # refused while it is being read
        assert big.exists()

        while state() == "processing":
            assert time.time() < deadline + 300, "indexing did not finish"
            time.sleep(1)
        assert state() == "done"
        assert web.delete("/api/documents/big.pdf").status_code == 200
        assert not big.exists()
        assert store.count_chunks(client, str(big)) == 0
