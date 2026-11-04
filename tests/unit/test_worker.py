import pytest

from rag import config, ingest, store, worker


@pytest.fixture
def docs(docs_dir):
    docs_dir.mkdir(parents=True)
    return docs_dir


@pytest.fixture
def calls(monkeypatch):
    """Record store/ingest calls and let each test decide what the index contains."""
    record = {"deleted": [], "indexed": [], "sources": {}, "counts": {}}
    monkeypatch.setattr(store, "get_client", lambda: "client")
    monkeypatch.setattr(store, "ensure_collection", lambda client, size: None)
    monkeypatch.setattr(store, "indexed_sources", lambda client: record["sources"])
    monkeypatch.setattr(store, "delete_source", lambda client, source: record["deleted"].append(source))
    monkeypatch.setattr(store, "count_chunks", lambda client, source: record["counts"].get(source, 0))
    monkeypatch.setattr(ingest, "build_pipeline", lambda: ("converter", "chunker"))
    return record


def run(w, name):
    w._process("client", "converter", "chunker", name)


# ---------- purge_stale ----------

def test_purge_removes_only_entries_whose_file_is_gone(docs, calls):
    (docs / "kept.pdf").write_bytes(b"x")
    calls["sources"] = {str(docs / "kept.pdf"): 4, str(docs / "gone.pdf"): 2}
    assert worker.purge_stale("client") == 1
    assert calls["deleted"] == [str(docs / "gone.pdf")]


def test_purge_refuses_to_wipe_the_index_when_the_folder_looks_empty(docs, calls):
    calls["sources"] = {str(docs / "a.pdf"): 4}
    assert worker.purge_stale("client") == 0
    assert calls["deleted"] == []


# ---------- job processing ----------

def test_successful_job_is_marked_done_with_chunk_count(docs, calls, monkeypatch):
    (docs / "a.txt").write_bytes(b"x")
    monkeypatch.setattr(ingest, "index_file", lambda *args: 7)
    w = worker.IndexWorker()
    run(w, "a.txt")
    assert w.snapshot()["a.txt"]["state"] == "done" and w.snapshot()["a.txt"]["chunks"] == 7


def test_unchanged_file_reports_the_stored_chunk_count(docs, calls, monkeypatch):
    (docs / "a.txt").write_bytes(b"x")
    calls["counts"][str(docs / "a.txt")] = 5
    monkeypatch.setattr(ingest, "index_file", lambda *args: None)
    w = worker.IndexWorker()
    run(w, "a.txt")
    assert w.snapshot()["a.txt"]["chunks"] == 5


def test_failed_job_keeps_the_error_and_does_not_stop_the_worker(docs, calls, monkeypatch):
    (docs / "bad.pdf").write_bytes(b"x")
    (docs / "good.txt").write_bytes(b"x")

    def index(client, converter, chunker, path):
        if path.name == "bad.pdf":
            raise ValueError("no text could be extracted")
        return 1

    monkeypatch.setattr(ingest, "index_file", index)
    w = worker.IndexWorker()
    run(w, "bad.pdf")
    run(w, "good.txt")
    snapshot = w.snapshot()
    assert snapshot["bad.pdf"]["state"] == "failed" and "no text" in snapshot["bad.pdf"]["error"]
    assert snapshot["good.txt"]["state"] == "done"


def test_file_deleted_while_waiting_in_the_queue_is_skipped(docs, calls, monkeypatch):
    monkeypatch.setattr(ingest, "index_file", lambda *args: pytest.fail("must not index a missing file"))
    w = worker.IndexWorker()
    run(w, "missing.txt")
    assert w.snapshot() == {}


def test_file_deleted_while_being_indexed_leaves_nothing_behind(docs, calls, monkeypatch):
    path = docs / "a.txt"
    path.write_bytes(b"x")

    def index(client, converter, chunker, p):
        p.unlink()  # the user deletes it mid-way
        return 3

    monkeypatch.setattr(ingest, "index_file", index)
    w = worker.IndexWorker()
    run(w, "a.txt")
    assert calls["deleted"] == [str(path)]
    assert "a.txt" not in w.snapshot()


def test_processing_state_is_visible_with_elapsed_seconds(docs, calls, monkeypatch):
    (docs / "a.txt").write_bytes(b"x")
    w = worker.IndexWorker()
    seen = {}

    def index(client, converter, chunker, path):
        seen["processing"] = w.is_processing("a.txt")
        seen["info"] = w.snapshot()["a.txt"]
        return 1

    monkeypatch.setattr(ingest, "index_file", index)
    run(w, "a.txt")
    assert seen["processing"] and seen["info"]["state"] == "processing" and seen["info"]["seconds"] >= 0
    assert not w.is_processing("a.txt")


# ---------- startup ----------

def test_startup_failure_is_reported_instead_of_crashing(docs, calls, monkeypatch):
    def broken(client, size):
        raise ConnectionError("qdrant is down")

    monkeypatch.setattr(store, "ensure_collection", broken)
    w = worker.IndexWorker()
    w._run()  # returns after reporting
    assert "qdrant is down" in w.error


def test_list_files_ignores_unsupported_lock_and_hidden_entries(docs):
    for name in ("a.pdf", "b.docx", "~$c.docx", "d.exe"):
        (docs / name).write_bytes(b"x")
    (docs / ".uploading").mkdir()
    assert [p.name for p in worker.list_files()] == ["a.pdf", "b.docx"]
    assert config.DOCS_DIR == docs
