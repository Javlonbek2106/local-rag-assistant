"""The web API and the command line against a real PostgreSQL (indexing itself is not run here)."""
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from rag import config, db, storage, store
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


def test_upload_is_stored_and_visible_to_workers(client):
    assert upload(client, "a.txt")["accepted"]
    doc = db.get_document("a.txt")
    assert doc["state"] == "queued"
    assert storage.path_for(doc["id"], "a.txt").read_bytes() == b"matn"
    assert db.claim_next("w1")["name"] == "a.txt"  # exactly what a worker would do


def test_listing_survives_an_api_restart(client, storage_dir):
    upload(client, "a.txt")
    with TestClient(create_app(db), base_url=f"http://127.0.0.1:{config.PORT}") as fresh:  # a new API process
        names = [d["name"] for d in fresh.get("/api/documents").json()["documents"]]
    assert names == ["a.txt"]  # the queue and the list live in the database, not in the API process


def test_a_document_being_indexed_cannot_be_replaced_or_deleted(client):
    upload(client, "a.txt", b"old")
    db.claim_next("w1")
    assert not upload(client, "a.txt", b"new")["accepted"]
    assert client.delete("/api/documents/a.txt").status_code == 409
    doc = db.get_document("a.txt")
    assert storage.path_for(doc["id"], "a.txt").read_bytes() == b"old"


def test_delete_cleans_the_database_the_disk_and_the_index(client):
    upload(client, "a.txt")
    doc_id = db.get_document("a.txt")["id"]
    assert client.delete("/api/documents/a.txt").status_code == 200
    assert db.get_document("a.txt") is None
    assert not storage.path_for(doc_id, "a.txt").exists()
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
    assert run().returncode == 0  # running it again is harmless
    assert [d["name"] for d in db.list_documents()] == ["a.txt", "b.md"]
