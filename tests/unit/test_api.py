import json

import pytest
from fastapi.testclient import TestClient

from rag import answer, config, storage
from rag.api import create_app
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

def test_startup_migrates_and_cleans_interrupted_uploads(storage_dir, repo, qdrant):
    (storage_dir / ".uploading").mkdir(parents=True)
    (storage_dir / ".uploading" / "half").write_bytes(b"x")
    with TestClient(create_app(repo), base_url=OWN_ORIGIN):
        pass
    assert repo.migrated
    assert not (storage_dir / ".uploading" / "half").exists()


# ---------- upload ----------

def upload(client, name, content=b"hello"):
    return client.post("/api/documents", files=[("files", (name, content))]).json()["results"][0]


def test_upload_saves_the_file_by_id_and_queues_it(client, repo, storage_dir):
    assert upload(client, "qoidalar.txt", b"matn") == {"name": "qoidalar.txt", "accepted": True}
    doc = repo.docs["qoidalar.txt"]
    assert doc["state"] == "queued" and doc["size_bytes"] == 4
    assert storage.path_for(doc["id"], "qoidalar.txt").read_bytes() == b"matn"
    assert list((storage_dir / ".uploading").iterdir()) == []  # the temp file was moved, not copied


def test_upload_rejects_unsupported_and_empty(client, repo):
    assert not upload(client, "virus.exe")["accepted"]
    assert not upload(client, "bosh.txt", b"")["accepted"]
    assert repo.docs == {}


def test_upload_rejects_oversized_file(client, monkeypatch, repo, storage_dir):
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 10)
    result = upload(client, "katta.txt", b"x" * 11)
    assert not result["accepted"] and "MB" in result["error"]
    assert repo.docs == {} and list((storage_dir / ".uploading").iterdir()) == []


def test_upload_never_writes_outside_the_storage_folder(client, repo, storage_dir):
    result = upload(client, "../../escape.txt")
    assert result["name"] == "escape.txt"
    assert storage.path_for(repo.docs["escape.txt"]["id"], "escape.txt").exists()
    assert not (storage_dir.parent / "escape.txt").exists()


def test_upload_is_refused_while_the_same_file_is_being_read(client, repo, storage_dir):
    upload(client, "a.txt", b"old")
    repo.docs["a.txt"]["state"] = "processing"
    result = upload(client, "a.txt", b"new")
    assert not result["accepted"]
    assert storage.path_for(repo.docs["a.txt"]["id"], "a.txt").read_bytes() == b"old"
    assert list((storage_dir / ".uploading").iterdir()) == []  # the refused upload leaves no temp file


def test_re_upload_replaces_the_file_but_keeps_the_document(client, repo):
    upload(client, "a.txt", b"old")
    doc_id = repo.docs["a.txt"]["id"]
    upload(client, "a.txt", b"new")
    assert repo.docs["a.txt"]["id"] == doc_id
    assert storage.path_for(doc_id, "a.txt").read_bytes() == b"new"


# ---------- delete ----------

def test_delete_removes_the_record_the_file_and_the_index_entries(client, repo, storage_dir, qdrant):
    upload(client, "a.txt")
    doc_id = repo.docs["a.txt"]["id"]
    assert client.delete("/api/documents/a.txt").status_code == 200
    assert "a.txt" not in repo.docs
    assert not storage.path_for(doc_id, "a.txt").exists()
    assert [f.must[0].match.value for f in qdrant.deleted_filters] == [str(doc_id)]  # the index is keyed by id


def test_delete_is_refused_while_the_file_is_being_read(client, repo, qdrant):
    upload(client, "a.txt")
    repo.docs["a.txt"]["state"] = "processing"
    assert client.delete("/api/documents/a.txt").status_code == 409
    assert "a.txt" in repo.docs and qdrant.deleted_filters == []


def test_delete_of_an_unknown_document_is_harmless(client, qdrant):
    assert client.delete("/api/documents/never.txt").status_code == 200
    assert qdrant.deleted_filters == []


def test_delete_rejects_unsupported_names(client):
    assert client.delete("/api/documents/evil.exe").status_code == 400


# ---------- listing ----------

def test_listing_shows_state_chunks_and_time(client, repo):
    upload(client, "a.txt")
    upload(client, "b.txt")
    repo.docs["a.txt"].update(state="done", chunks=3)
    repo.docs["b.txt"].update(state="processing", seconds=12)
    rows = {r["name"]: r for r in client.get("/api/documents").json()["documents"]}
    assert rows["a.txt"]["state"] == "done" and rows["a.txt"]["chunks"] == 3 and rows["a.txt"]["size"] == 5
    assert rows["b.txt"]["state"] == "processing" and rows["b.txt"]["seconds"] == 12
    assert client.get("/api/documents").json()["queued"] == 1


def test_a_document_that_is_still_uploading_is_shown_as_queued(client, repo):
    repo.upsert_document("a.txt", 5)  # saved nowhere yet
    assert client.get("/api/documents").json()["documents"][0]["state"] == "queued"


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


def test_ask_shows_only_the_sources_the_answer_cites(client, monkeypatch):
    hits = [make_hit(0.8, "a.txt", "birinchi"), make_hit(0.7, "b.txt", "ikkinchi"), make_hit(0.6, "c.txt", "uchinchi")]
    monkeypatch.setattr(answer, "retrieve", lambda q: hits)
    monkeypatch.setattr(answer, "stream_answer", lambda q, h: iter(["Javob [2]"]))
    final = ndjson(client.post("/api/ask", json={"question": "?"}))[-1]
    assert [(s["n"], s["file"]) for s in final["sources"]] == [(2, "b.txt")]


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
