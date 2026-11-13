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
    monkeypatch.setattr(answer, "retrieve", lambda q, repo=None: [])
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
    assert upload(client, "qoidalar.txt", b"matn") == {"name": "qoidalar.txt", "accepted": True, "version": 1,
                                                       "unchanged": False}
    doc = repo.docs["qoidalar.txt"]
    assert doc["state"] == "queued" and doc["size_bytes"] == 4
    version = repo.versions[0]
    assert version["status"] == "QUEUED" and version["version_number"] == 1
    assert storage.resolve(version["file_path"]).read_bytes() == b"matn"  # saved under its own version folder
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
    assert storage.resolve(repo.versions[0]["file_path"]).exists()
    assert not (storage_dir.parent / "escape.txt").exists()


def test_upload_while_the_same_file_is_being_read_becomes_the_next_version(client, repo, storage_dir):
    upload(client, "a.txt", b"old")
    repo.set_status("a.txt", 1, "PROCESSING")
    result = upload(client, "a.txt", b"new")
    assert result["accepted"] and result["version"] == 2
    first, second = repo.versions
    assert storage.resolve(first["file_path"]).read_bytes() == b"old"  # the version being read is untouched
    assert storage.resolve(second["file_path"]).read_bytes() == b"new"
    assert first["status"] == "PROCESSING" and second["status"] == "QUEUED"
    assert list((storage_dir / ".uploading").iterdir()) == []


def test_re_upload_adds_a_version_and_keeps_the_document_and_the_old_file(client, repo):
    upload(client, "a.txt", b"old")
    doc_id = repo.docs["a.txt"]["id"]
    upload(client, "a.txt", b"new")
    assert repo.docs["a.txt"]["id"] == doc_id
    assert [storage.resolve(v["file_path"]).read_bytes() for v in repo.versions] == [b"old", b"new"]


def test_uploading_the_same_file_again_is_reported_as_unchanged_and_adds_nothing(client, repo, storage_dir):
    upload(client, "a.txt", b"same")
    again = upload(client, "a.txt", b"same")
    assert again == {"name": "a.txt", "accepted": True, "version": 1, "unchanged": True}
    assert len(repo.versions) == 1 and list((storage_dir / ".uploading").iterdir()) == []


# ---------- delete ----------

def test_delete_removes_the_record_the_file_and_the_index_entries(client, repo, storage_dir, qdrant):
    upload(client, "a.txt")
    doc_id = repo.docs["a.txt"]["id"]
    assert client.delete("/api/documents/a.txt").status_code == 200
    assert "a.txt" not in repo.docs
    assert not (storage_dir / "documents" / str(doc_id)).exists()  # every version's file goes
    assert [f.must[0].match.value for f in qdrant.deleted_filters] == [str(doc_id)]  # the index is keyed by id


def test_delete_is_refused_while_the_file_is_being_read(client, repo, qdrant):
    upload(client, "a.txt")
    repo.set_status("a.txt", 1, "PROCESSING")
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
    repo.set_status("a.txt", 1, "READY", chunks=3, active=True)
    repo.set_status("b.txt", 1, "PROCESSING")
    repo.docs["b.txt"]["seconds"] = 12
    rows = {r["name"]: r for r in client.get("/api/documents").json()["documents"]}
    assert rows["a.txt"]["state"] == "done" and rows["a.txt"]["chunks"] == 3 and rows["a.txt"]["size"] == 5
    assert rows["b.txt"]["state"] == "processing" and rows["b.txt"]["seconds"] == 12
    assert client.get("/api/documents").json()["queued"] == 1


def test_a_document_that_is_still_uploading_is_shown_as_queued(client, repo):
    repo.create_version("a.txt", 5, "hash")  # saved nowhere yet
    assert client.get("/api/documents").json()["documents"][0]["state"] == "queued"


# ---------- ask ----------

def test_ask_streams_tokens_then_final_with_sources(client, monkeypatch):
    hits = [make_hit(0.8, "a.txt", "Lola-5G", [2])]
    monkeypatch.setattr(answer, "retrieve", lambda q, repo=None: hits)
    monkeypatch.setattr(answer, "stream_answer", lambda q, h: iter(["Lola", "-5G [1]"]))
    events = ndjson(client.post("/api/ask", json={"question": "Wi-Fi?"}))
    assert [e["token"] for e in events[:-1]] == ["Lola", "-5G [1]"]
    final = events[-1]
    assert final["found"] and final["final"] == "Lola-5G [1]"
    assert final["sources"] == [{"n": 1, "file": "a.txt", "pages": [2], "score": 0.8, "text": "Lola-5G"}]


def test_ask_shows_only_the_sources_the_answer_cites(client, monkeypatch):
    hits = [make_hit(0.8, "a.txt", "birinchi"), make_hit(0.7, "b.txt", "ikkinchi"), make_hit(0.6, "c.txt", "uchinchi")]
    monkeypatch.setattr(answer, "retrieve", lambda q, repo=None: hits)
    monkeypatch.setattr(answer, "stream_answer", lambda q, h: iter(["Javob [2]"]))
    final = ndjson(client.post("/api/ask", json={"question": "?"}))[-1]
    assert [(s["n"], s["file"]) for s in final["sources"]] == [(2, "b.txt")]


def test_ask_without_relevant_chunks_says_not_found_and_shows_no_sources(client, monkeypatch):
    monkeypatch.setattr(answer, "retrieve", lambda q, repo=None: [])
    final = ndjson(client.post("/api/ask", json={"question": "?"}))[-1]
    assert final == {"final": answer.NOT_FOUND, "found": False, "sources": []}


def test_ask_when_model_itself_says_not_found_drops_sources_and_stray_citation(client, monkeypatch):
    monkeypatch.setattr(answer, "retrieve", lambda q, repo=None: [make_hit()])
    monkeypatch.setattr(answer, "stream_answer", lambda q, h: iter([answer.NOT_FOUND, " [1]"]))
    final = ndjson(client.post("/api/ask", json={"question": "?"}))[-1]
    assert final == {"final": answer.NOT_FOUND, "found": False, "sources": []}


def test_ask_reports_retrieval_failure_as_503(client, monkeypatch):
    def boom(question, repo=None):
        raise ConnectionError("qdrant down")

    monkeypatch.setattr(answer, "retrieve", boom)
    response = client.post("/api/ask", json={"question": "?"})
    assert response.status_code == 503 and "qdrant down" in response.json()["detail"]


def test_ask_reports_generation_failure_as_event(client, monkeypatch):
    def broken(question, hits):
        yield "Bo'l"
        raise RuntimeError("ollama stopped")

    monkeypatch.setattr(answer, "retrieve", lambda q, repo=None: [make_hit()])
    monkeypatch.setattr(answer, "stream_answer", broken)
    events = ndjson(client.post("/api/ask", json={"question": "?"}))
    assert events[-1] == {"error": "Javob yozilmadi: ollama stopped"}


@pytest.mark.parametrize("question", ["", "x" * 2001])
def test_ask_validates_question_length(client, question):
    assert client.post("/api/ask", json={"question": question}).status_code == 422


# ---------- versions ----------

def test_listing_shows_the_active_and_the_latest_version_and_why_the_newest_failed(client, repo):
    upload(client, "a.txt", b"one")
    repo.set_status("a.txt", 1, "READY", chunks=3, active=True)
    upload(client, "a.txt", b"two")
    repo.set_status("a.txt", 2, "FAILED", error="Qdrant to'xtadi")
    row = client.get("/api/documents").json()["documents"][0]
    assert (row["state"], row["version"], row["latest_version"]) == ("done", 1, 2)  # version 1 keeps serving
    assert row["update_error"] == "Qdrant to'xtadi" and row["error"] is None


def test_versions_endpoint_lists_every_version_newest_first(client, repo):
    upload(client, "a.txt", b"one")
    repo.set_status("a.txt", 1, "READY", chunks=3, active=True)
    upload(client, "a.txt", b"two")
    body = client.get("/api/documents/a.txt/versions").json()
    assert [(v["version"], v["status"], v["active"]) for v in body["versions"]] == [(2, "QUEUED", False),
                                                                                     (1, "READY", True)]
    assert client.get("/api/documents/never.txt/versions").status_code == 404


def seed_two_versions(client, repo, qdrant):
    """Version 1 superseded (its 2 chunks still in the index), version 2 active."""
    upload(client, "a.txt", b"one")
    upload(client, "a.txt", b"two")
    v1, v2 = repo.versions
    repo.set_status("a.txt", 1, "READY", chunks=2, active=True)
    repo.set_status("a.txt", 2, "READY", chunks=1, active=True)
    for version, texts in ((v1, ["x", "y"]), (v2, ["z"])):
        for i, text in enumerate(texts):
            qdrant.points[f"{version['version_number']}-{i}"] = ([0.1], {
                "document_id": str(version["document_id"]), "version_id": str(version["id"]),
                "file_hash": version["file_hash"], "chunks_total": len(texts), "text": text})
    return v1, v2


def test_rollback_makes_the_earlier_version_active_without_touching_the_index(client, repo, qdrant):
    v1, v2 = seed_two_versions(client, repo, qdrant)
    response = client.post("/api/documents/a.txt/rollback", json={"version": 1})
    assert response.status_code == 200 and response.json()["version"] == 1
    assert repo.active_version_ids() == [str(v1["id"])]
    assert repo.get_version("a.txt", 2)["status"] == "SUPERSEDED"
    assert len(qdrant.points) == 3 and qdrant.deleted_filters == []  # nothing re-embedded, nothing deleted


def test_rollback_is_refused_when_the_old_vectors_are_gone(client, repo, qdrant):
    v1, v2 = seed_two_versions(client, repo, qdrant)
    del qdrant.points["1-1"]  # one chunk of version 1 is missing
    response = client.post("/api/documents/a.txt/rollback", json={"version": 1})
    assert response.status_code == 409 and "to'liq emas" in response.json()["detail"]
    assert repo.active_version_ids() == [str(v2["id"])]  # still serving version 2


@pytest.mark.parametrize("version, status", [(2, 409), (3, 404)])
def test_rollback_refuses_the_active_version_and_unknown_ones(client, repo, qdrant, version, status):
    seed_two_versions(client, repo, qdrant)
    assert client.post("/api/documents/a.txt/rollback", json={"version": version}).status_code == status


def test_rollback_refuses_a_version_that_never_became_active(client, repo, qdrant):
    seed_two_versions(client, repo, qdrant)
    upload(client, "a.txt", b"three")  # queued, never active
    response = client.post("/api/documents/a.txt/rollback", json={"version": 3})
    assert response.status_code == 409 and "QUEUED" in response.json()["detail"]
