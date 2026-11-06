import os
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from rag import config, db, store
from rag.api import create_app


def pytest_collection_modifyitems(config, items):
    """Tests that need real services only run when switched on: integration/e2e with =1, db with a database URL."""
    switches = (("integration", "RAG_INTEGRATION", "1"), ("e2e", "RAG_E2E", "1"), ("db", "RAG_TEST_DATABASE_URL", None))
    for marker, switch, wanted in switches:
        value = os.getenv(switch)
        if value == wanted if wanted else bool(value):
            continue
        skip = pytest.mark.skip(reason=f"set {switch}{'=' + wanted if wanted else ' to a PostgreSQL URL'} to run")
        for item in items:
            if marker in item.keywords:
                item.add_marker(skip)


@pytest.fixture(autouse=True)
def no_reranker(monkeypatch):
    """Unit tests must not load the cross-encoder model; tests that want re-ranking switch it on themselves."""
    monkeypatch.setattr(config, "RERANK_MODEL", "")


class FakeRepo:
    """In-memory stand-in for rag.db, so API tests need no PostgreSQL."""

    Busy = db.Busy

    def __init__(self):
        self.docs = {}  # name -> dict
        self.migrated = False

    def migrate(self):
        self.migrated = True

    def upsert_document(self, name, size_bytes):
        found = self.docs.get(name)
        if found and found["state"] == "processing":
            raise db.Busy(name)
        doc_id = found["id"] if found else uuid.uuid4()
        self.docs[name] = {"id": doc_id, "name": name, "size_bytes": size_bytes, "state": "uploading",
                           "chunks": None, "error": None, "seconds": None}
        return doc_id

    def enqueue(self, doc_id):
        for doc in self.docs.values():
            if doc["id"] == doc_id and doc["state"] == "uploading":
                doc["state"] = "queued"
                return True
        return False

    def list_documents(self):
        return [self.docs[name] for name in sorted(self.docs)]

    def queue_length(self):
        return sum(d["state"] in ("queued", "processing") for d in self.docs.values())

    def delete_document(self, name):
        found = self.docs.get(name)
        if found is None:
            return None
        if found["state"] == "processing":
            raise db.Busy(name)
        del self.docs[name]
        return found["id"]


class FakeQdrant:
    def __init__(self):
        self.deleted_filters = []

    def delete(self, collection, points_selector):
        self.deleted_filters.append(points_selector.filter)


@pytest.fixture
def storage_dir(tmp_path, monkeypatch):
    path = tmp_path / "storage"
    monkeypatch.setattr(config, "STORAGE_DIR", path)
    return path


@pytest.fixture
def repo():
    return FakeRepo()


@pytest.fixture
def qdrant(monkeypatch):
    fake = FakeQdrant()
    monkeypatch.setattr(store, "get_client", lambda: fake)
    monkeypatch.setattr(store, "ensure_collection", lambda client, size: None)
    return fake


@pytest.fixture
def client(storage_dir, repo, qdrant):
    with TestClient(create_app(repo), base_url=f"http://127.0.0.1:{config.PORT}") as test_client:
        yield test_client


@pytest.fixture(scope="session")
def database():
    """A throw-away schema in the test database, migrated once and dropped at the end."""
    url = os.environ["RAG_TEST_DATABASE_URL"]
    schema = f"rag_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    mp = pytest.MonkeyPatch()
    mp.setattr(config, "DATABASE_URL", url)
    mp.setattr(config, "DB_SCHEMA", schema)
    db.pool.cache_clear()
    db.migrate()
    yield schema
    db.close()
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA {schema} CASCADE")
    mp.undo()
