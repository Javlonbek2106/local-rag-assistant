import json

import pytest

from rag import answer, config
from tests.helpers import make_hit

OWN_ORIGIN = f"http://127.0.0.1:{config.PORT}"


def ndjson(response):
    return [json.loads(line) for line in response.text.splitlines() if line]


# ---------- security guard ----------

def test_foreign_host_is_rejected(client):
    assert client.get("/api/documents", headers={"host": "evil.com"}).status_code == 403


def test_foreign_origin_cannot_write(client):
    assert client.post("/api/ask", json={"question": "x"}, headers={"origin": "http://evil.com"}).status_code == 403
    assert client.delete("/api/documents/a.txt", headers={"origin": "http://evil.com"}).status_code == 403


def test_own_origin_and_no_origin_are_allowed(client, monkeypatch):
    monkeypatch.setattr(answer, "retrieve", lambda q: [])
    assert client.post("/api/ask", json={"question": "x"}, headers={"origin": OWN_ORIGIN}).status_code == 200
    assert client.post("/api/ask", json={"question": "x"}).status_code == 200  # non-browser client (e.g. .NET)


def test_security_headers_are_set(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"


# ---------- startup ----------

def test_startup_restores_queue_and_cleans_leftovers(docs_dir, worker, qdrant):
    from fastapi.testclient import TestClient

    from rag.api import create_app

    docs_dir.mkdir(parents=True)
    (docs_dir / "a.pdf").write_bytes(b"x")
    (docs_dir / "b.docx").write_bytes(b"x")
    (docs_dir / "~$lock.docx").write_bytes(b"x")
    (docs_dir / "notes.exe").write_bytes(b"x")
    (docs_dir / ".uploading").mkdir()
    (docs_dir / ".uploading" / "half").write_bytes(b"x")
    with TestClient(create_app(worker), base_url=f"http://127.0.0.1:{config.PORT}"):
        pass
    assert worker.started
    assert sorted(worker.submitted) == ["a.pdf", "b.docx"]
    assert not (docs_dir / ".uploading" / "half").exists()


# ---------- upload ----------

def upload(client, name, content=b"hello"):
    return client.post("/api/documents", files=[("files", (name, content))]).json()["results"][0]


def test_upload_saves_file_and_queues_it(client, worker, docs_dir):
    result = upload(client, "qoidalar.txt", b"matn")
    assert result == {"name": "qoidalar.txt", "accepted": True}
    assert (docs_dir / "qoidalar.txt").read_bytes() == b"matn"
    assert worker.submitted == ["qoidalar.txt"]
    assert list((docs_dir / ".uploading").iterdir()) == []  # temp file was moved, not copied


def test_upload_rejects_unsupported_and_empty(client, worker, docs_dir):
    assert not upload(client, "virus.exe")["accepted"]
    assert not upload(client, "bosh.txt", b"")["accepted"]
    assert worker.submitted == []
    assert not (docs_dir / "virus.exe").exists()


def test_upload_rejects_oversized_file(client, monkeypatch, docs_dir):
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 10)
    result = upload(client, "katta.txt", b"x" * 11)
    assert not result["accepted"] and "MB" in result["error"]
    assert not (docs_dir / "katta.txt").exists()
    assert list((docs_dir / ".uploading").iterdir()) == []


def test_upload_never_writes_outside_documents_folder(client, docs_dir):
    result = upload(client, "../../escape.txt")
    assert result["name"] == "escape.txt"
    assert (docs_dir / "escape.txt").exists()
    assert not (docs_dir.parent / "escape.txt").exists()


def test_upload_is_refused_while_the_same_file_is_being_read(client, worker, docs_dir):
    docs_dir.joinpath("a.txt").write_bytes(b"old")
    worker.processing.add("a.txt")
    result = upload(client, "a.txt", b"new")
    assert not result["accepted"]
    assert docs_dir.joinpath("a.txt").read_bytes() == b"old"


# ---------- delete ----------

def test_delete_removes_file_and_index_entries(client, worker, docs_dir, qdrant):
    docs_dir.joinpath("a.txt").write_bytes(b"x")
    assert client.delete("/api/documents/a.txt").status_code == 200
    assert not docs_dir.joinpath("a.txt").exists()
    assert len(qdrant.deleted_filters) == 1
    assert qdrant.deleted_filters[0].must[0].match.value == str(docs_dir / "a.txt")
    assert worker.forgotten == ["a.txt"]


def test_delete_is_refused_while_the_file_is_being_read(client, worker, docs_dir, qdrant):
    docs_dir.joinpath("a.txt").write_bytes(b"x")
    worker.processing.add("a.txt")
    assert client.delete("/api/documents/a.txt").status_code == 409
    assert docs_dir.joinpath("a.txt").exists()
    assert qdrant.deleted_filters == []


def test_delete_rejects_unsupported_names(client):
    assert client.delete("/api/documents/evil.exe").status_code == 400


# ---------- listing ----------

def test_listing_combines_disk_files_with_status(client, worker, docs_dir):
    docs_dir.joinpath("a.txt").write_bytes(b"12345")
    docs_dir.joinpath("b.txt").write_bytes(b"1")
    worker.status = {"a.txt": {"state": "done", "chunks": 3, "error": None, "seconds": None}}
    worker.error = "Ollama yo'q"
    data = client.get("/api/documents").json()
    rows = {row["name"]: row for row in data["documents"]}
    assert rows["a.txt"]["state"] == "done" and rows["a.txt"]["chunks"] == 3 and rows["a.txt"]["size"] == 5
    assert rows["b.txt"]["state"] == "queued"  # on disk but not seen by the worker yet
    assert data["error"] == "Ollama yo'q"


# ---------- ask ----------

def test_ask_streams_tokens_then_final_with_sources(client, monkeypatch):
    hits = [make_hit(0.8, "a.txt", "Lola-5G", [2])]
    monkeypatch.setattr(answer, "retrieve", lambda q: hits)
    monkeypatch.setattr(answer, "stream_answer", lambda q, h: iter(["Lola", "-5G [1]"]))
    events = ndjson(client.post("/api/ask", json={"question": "Wi-Fi?"}))
    assert [e["token"] for e in events[:-1]] == ["Lola", "-5G [1]"]
    final = events[-1]
    assert final["found"] and final["final"] == "Lola-5G [1]"
    assert final["sources"] == [{"n": 1, "file": "a.txt", "pages": [2], "score": 0.8, "text": "Lola-5G"}]


def test_ask_without_relevant_chunks_says_not_found_and_shows_no_sources(client, monkeypatch):
    monkeypatch.setattr(answer, "retrieve", lambda q: [])
    final = ndjson(client.post("/api/ask", json={"question": "?"}))[-1]
    assert final == {"final": answer.NOT_FOUND, "found": False, "sources": []}


def test_ask_when_model_itself_says_not_found_drops_sources_and_stray_citation(client, monkeypatch):
    monkeypatch.setattr(answer, "retrieve", lambda q: [make_hit()])
    monkeypatch.setattr(answer, "stream_answer", lambda q, h: iter([answer.NOT_FOUND, " [1]"]))
    final = ndjson(client.post("/api/ask", json={"question": "?"}))[-1]
    assert final == {"final": answer.NOT_FOUND, "found": False, "sources": []}


def test_ask_reports_retrieval_failure_as_503(client, monkeypatch):
    def boom(question):
        raise ConnectionError("qdrant down")

    monkeypatch.setattr(answer, "retrieve", boom)
    response = client.post("/api/ask", json={"question": "?"})
    assert response.status_code == 503 and "qdrant down" in response.json()["detail"]


def test_ask_reports_generation_failure_as_event(client, monkeypatch):
    def broken(question, hits):
        yield "Bo'l"
        raise RuntimeError("ollama stopped")

    monkeypatch.setattr(answer, "retrieve", lambda q: [make_hit()])
    monkeypatch.setattr(answer, "stream_answer", broken)
    events = ndjson(client.post("/api/ask", json={"question": "?"}))
    assert events[-1] == {"error": "Javob yozilmadi: ollama stopped"}


@pytest.mark.parametrize("question", ["", "x" * 2001])
def test_ask_validates_question_length(client, question):
    assert client.post("/api/ask", json={"question": question}).status_code == 422
