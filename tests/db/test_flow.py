"""The web API and the command line against a real PostgreSQL (indexing itself is not run here)."""
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from rag import answer, config, db, llm, storage, store
from rag.api import create_app
from tests.conftest import FakeQdrant

pytestmark = pytest.mark.db


@pytest.fixture
def client(storage_dir, monkeypatch):
    fake = FakeQdrant()
    monkeypatch.setattr(store, "get_client", lambda: fake)
    monkeypatch.setattr(store, "ensure_collection", lambda c, size: None)
    with TestClient(create_app(db), base_url=f"http://127.0.0.1:{config.PORT}") as test_client:
        test_client.qdrant = fake
        yield test_client


def upload(client, name, content=b"matn"):
    return client.post("/api/documents", files=[("files", (name, content))]).json()["results"][0]


def activate(name, number, chunks=1, worker="w1"):
    """Do what a worker does with the queued version `number`: claim, verify, activate."""
    job = db.claim_next(worker)
    assert job["name"] == name and job["version_number"] == number
    assert db.set_stage(job["id"], worker, "VERIFYING")
    assert db.activate(job["id"], worker, chunks) == "activated"
    return job


def add_chunks(client, job, texts):
    """Put a version's chunks into the fake index the way the worker would."""
    for i, text in enumerate(texts):
        client.qdrant.points[f"{job['version_number']}-{i}"] = ([0.1], {
            "source": str(job["document_id"]), "document_id": str(job["document_id"]), "version_id": str(job["id"]),
            "file_name": job["file_name"], "file_hash": job["file_hash"], "chunks_total": len(texts),
            "pages": [], "text": text})


def test_upload_is_stored_and_visible_to_workers(client):
    assert upload(client, "a.txt")["accepted"]
    doc = db.get_document("a.txt")
    assert doc["state"] == "queued"
    version = db.get_version("a.txt", 1)
    assert storage.resolve(version["file_path"]).read_bytes() == b"matn"
    assert version["file_path"].startswith(f"documents/{doc['id']}/versions/{version['id']}/")
    assert db.claim_next("w1")["name"] == "a.txt"  # exactly what a worker would do


def test_listing_survives_an_api_restart(client, storage_dir):
    upload(client, "a.txt")
    with TestClient(create_app(db), base_url=f"http://127.0.0.1:{config.PORT}") as fresh:  # a new API process
        names = [d["name"] for d in fresh.get("/api/documents").json()["documents"]]
    assert names == ["a.txt"]  # the queue and the list live in the database, not in the API process


def test_a_document_being_indexed_gets_a_new_version_and_cannot_be_deleted(client):
    upload(client, "a.txt", b"old")
    db.claim_next("w1")
    result = upload(client, "a.txt", b"new")
    assert result["accepted"] and result["version"] == 2
    assert client.delete("/api/documents/a.txt").status_code == 409
    assert storage.resolve(db.get_version("a.txt", 1)["file_path"]).read_bytes() == b"old"  # never overwritten
    assert storage.resolve(db.get_version("a.txt", 2)["file_path"]).read_bytes() == b"new"


def test_delete_cleans_the_database_the_disk_and_the_index(client, storage_dir):
    upload(client, "a.txt")
    upload(client, "a.txt", b"second version")
    doc_id = db.get_document("a.txt")["id"]
    assert client.delete("/api/documents/a.txt").status_code == 200
    assert db.get_document("a.txt") is None and db.list_versions("a.txt") is None
    with db.pool().connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM document_versions").fetchone()["n"] == 0
    assert not (storage_dir / "documents" / str(doc_id)).exists()  # both versions' files are gone
    assert [f.must[0].match.value for f in client.qdrant.deleted_filters] == [str(doc_id)]


def test_command_line_import_queues_files_like_an_upload(tmp_path, database, storage_dir):
    incoming = tmp_path / "incoming"
    (incoming / "sub").mkdir(parents=True)
    (incoming / "a.txt").write_bytes(b"bir")
    (incoming / "sub" / "b.md").write_bytes(b"ikki")
    (incoming / "sub" / "a.txt").write_bytes(b"takror nom")
    (incoming / "old.doc").write_bytes(b"x")
    (incoming / "~$lock.docx").write_bytes(b"x")
    env = {**os.environ, "RAG_DATABASE_URL": config.DATABASE_URL, "RAG_DB_SCHEMA": database,
           "RAG_STORAGE": str(storage_dir), "PYTHONIOENCODING": "utf-8"}

    def run():
        return subprocess.run([sys.executable, "-m", "rag.ingest", str(incoming)], env=env, cwd=config.ROOT,
                              capture_output=True, text=True, encoding="utf-8")

    result = run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "QUEUED    a.txt" in result.stdout and "QUEUED    b.md" in result.stdout
    assert "old.doc: unsupported format" in result.stdout and "same name" in result.stdout
    assert "lock" not in result.stdout  # Office lock files are skipped silently
    assert [d["name"] for d in db.list_documents()] == ["a.txt", "b.md"]
    assert all(d["state"] == "queued" for d in db.list_documents())
    again = run()  # running it again is harmless: unchanged files add no versions
    assert again.returncode == 0 and "unchanged" in again.stdout
    assert [d["name"] for d in db.list_documents()] == ["a.txt", "b.md"]
    assert [d["latest_version"] for d in db.list_documents()] == [1, 1]


# ---------- versions through the API ----------

def test_a_re_upload_is_a_new_version_while_the_old_one_stays_listed_and_active(client):
    upload(client, "a.txt", b"one")
    activate("a.txt", 1, chunks=7)
    assert upload(client, "a.txt", b"two") == {"name": "a.txt", "accepted": True, "version": 2, "unchanged": False}
    row = client.get("/api/documents").json()["documents"][0]
    assert (row["state"], row["version"], row["latest_version"], row["chunks"]) == ("queued", 1, 2, 7)
    versions = client.get("/api/documents/a.txt/versions").json()["versions"]
    assert [(v["version"], v["status"], v["active"]) for v in versions] == [(2, "QUEUED", False), (1, "READY", True)]


def test_uploading_the_same_file_again_changes_nothing(client):
    upload(client, "a.txt", b"same")
    activate("a.txt", 1)
    assert upload(client, "a.txt", b"same")["unchanged"] is True
    assert db.get_document("a.txt")["latest_version"] == 1 and db.queue_length() == 0


def test_a_question_is_answered_only_from_the_active_version(client, monkeypatch):
    # version 2 is indexed (its chunks are in Qdrant) but not verified yet, so it must stay invisible
    upload(client, "a.txt", b"one")
    first = activate("a.txt", 1)
    add_chunks(client, first, ["Narx: 100 so'm"])
    upload(client, "a.txt", b"two")
    second = db.claim_next("w1")
    add_chunks(client, second, ["Narx: 999 so'm", "Yangi bo'lim"])
    monkeypatch.setattr(llm, "embed", lambda texts: [[0.1]])
    monkeypatch.setattr(config, "MIN_SCORE", 0.0)
    assert [h.payload["text"] for h in answer.retrieve("narx?", client.qdrant)] == ["Narx: 100 so'm"]
    activate_second = (db.set_stage(second["id"], "w1", "VERIFYING"), db.activate(second["id"], "w1", 2))
    assert activate_second[1] == "activated"
    assert sorted(h.payload["text"] for h in answer.retrieve("narx?", client.qdrant)) == ["Narx: 999 so'm",
                                                                                          "Yangi bo'lim"]


def test_rollback_through_the_api_switches_what_questions_see(client, monkeypatch):
    upload(client, "a.txt", b"one")
    first = activate("a.txt", 1, chunks=1)
    add_chunks(client, first, ["eski matn"])
    upload(client, "a.txt", b"two")
    second = activate("a.txt", 2, chunks=1)
    add_chunks(client, second, ["yangi matn"])
    monkeypatch.setattr(llm, "embed", lambda texts: [[0.1]])
    monkeypatch.setattr(config, "MIN_SCORE", 0.0)
    assert [h.payload["text"] for h in answer.retrieve("?", client.qdrant)] == ["yangi matn"]
    assert client.post("/api/documents/a.txt/rollback", json={"version": 1}).status_code == 200
    assert [h.payload["text"] for h in answer.retrieve("?", client.qdrant)] == ["eski matn"]
    assert db.get_version("a.txt", 2)["status"] == "SUPERSEDED"
    assert client.post("/api/documents/a.txt/rollback", json={"version": 2}).status_code == 200  # and forward again
    assert [h.payload["text"] for h in answer.retrieve("?", client.qdrant)] == ["yangi matn"]
