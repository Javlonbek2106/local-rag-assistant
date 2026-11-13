import os
import uuid
from types import SimpleNamespace

import psycopg
import pytest
from fastapi.testclient import TestClient
from qdrant_client import models

from rag import config, db, storage, store
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
    """In-memory stand-in for rag.db, so API tests need no PostgreSQL. Versions and the document summary follow the
    same rules as the SQL (the database tests check those); `set_status` lets a test move a version along."""

    Busy = db.Busy
    VersionConflict = db.VersionConflict

    def __init__(self):
        self.docs = {}  # name -> document summary
        self.versions = []
        self.migrated = False

    def migrate(self):
        self.migrated = True

    def _refresh(self, doc):
        mine = [v for v in self.versions if v["document_id"] == doc["id"]]
        active = next((v for v in mine if v["active"]), None)
        statuses = {v["status"] for v in mine}
        latest = max(mine, key=lambda v: v["version_number"])
        if statuses & set(db.INFLIGHT):
            state = "processing"
        elif "QUEUED" in statuses:
            state = "queued"
        elif "UPLOADED" in statuses:
            state = "uploading"
        else:
            state = "done" if active else "failed"
        failed = latest["status"] == "FAILED"
        doc.update(state=state, chunks=active["chunk_count"] if active else None, size_bytes=latest["file_size"],
                   version=active["version_number"] if active else None, latest_version=latest["version_number"],
                   error=latest["error_message"] if state == "failed" and failed else None,
                   update_error=latest["error_message"] if state == "done" and failed else None)

    def create_version(self, name, size_bytes, file_hash):
        doc = self.docs.setdefault(name, {"id": uuid.uuid4(), "name": name, "seconds": None, "size_bytes": size_bytes})
        mine = [v for v in self.versions if v["document_id"] == doc["id"]]
        newest = max((v for v in mine if v["status"] != "FAILED"), key=lambda v: v["version_number"], default=None)
        if newest and newest["file_hash"] == file_hash:
            return {"id": newest["id"], "document_id": doc["id"], "version_number": newest["version_number"],
                    "file_path": newest["file_path"], "created": False}
        version_id = uuid.uuid4()
        version = {"id": version_id, "document_id": doc["id"], "name": name, "version_number": len(mine) + 1,
                   "file_name": name, "file_hash": file_hash, "file_size": size_bytes,
                   "file_path": storage.relative_path(doc["id"], version_id, name), "status": "UPLOADED",
                   "chunk_count": None, "error_message": None, "active": False, "created_at": None,
                   "indexed_at": None, "activated_at": None}
        self.versions.append(version)
        self._refresh(doc)
        return {"id": version_id, "document_id": doc["id"], "version_number": version["version_number"],
                "file_path": version["file_path"], "created": True}

    def _find(self, version_id):
        return next((v for v in self.versions if v["id"] == version_id), None)

    def set_status(self, name, number, status, chunks=None, active=None, error=None):
        """Test helper: put a version in a state (activating it, if asked, and demoting the previous active one)."""
        version = next(v for v in self.versions if v["name"] == name and v["version_number"] == number)
        if active:
            for other in self.versions:
                if other["document_id"] == version["document_id"] and other["active"]:
                    other.update(active=False, status="SUPERSEDED")
        version.update(status=status, chunk_count=chunks if chunks is not None else version["chunk_count"],
                       error_message=error, **({} if active is None else {"active": active}))
        self._refresh(self.docs[name])

    def enqueue(self, version_id):
        version = self._find(version_id)
        if version is None or version["status"] != "UPLOADED":
            return False
        version["status"] = "QUEUED"
        self._refresh(self.docs[version["name"]])
        return True

    def list_documents(self):
        return [self.docs[name] for name in sorted(self.docs)]

    def queue_length(self):
        return sum(d["state"] in ("queued", "processing") for d in self.docs.values())

    def delete_document(self, name):
        doc = self.docs.get(name)
        if doc is None:
            return None
        if doc["state"] == "processing":
            raise db.Busy(name)
        self.versions = [v for v in self.versions if v["document_id"] != doc["id"]]
        del self.docs[name]
        return doc["id"]

    def list_versions(self, name):
        if name not in self.docs:
            return None
        return sorted((v for v in self.versions if v["name"] == name), key=lambda v: -v["version_number"])

    def get_version(self, name, number):
        return next((v for v in self.versions if v["name"] == name and v["version_number"] == number), None)

    def active_version_ids(self):
        return [str(v["id"]) for v in self.versions if v["active"]]

    def versions_without_hash(self):
        return [v for v in self.versions if v["file_hash"] is None]

    def set_file_hash(self, version_id, file_hash):
        self._find(version_id)["file_hash"] = file_hash

    def switch_active(self, version_id):
        version = self._find(version_id)
        if version is None or version["status"] != "SUPERSEDED" or version["chunk_count"] is None:
            raise db.VersionConflict("Bu versiyaga qaytib bo'lmaydi")
        self.set_status(version["name"], version["version_number"], "READY", active=True)


def _matches(payload, flt):
    """The part of Qdrant's filter language the code uses: must-conditions of value, any-of and "field is empty"."""
    for cond in flt.must or []:
        if isinstance(cond, models.IsEmptyCondition):
            if payload.get(cond.is_empty.key) not in (None, "", []):
                return False
        elif hasattr(cond.match, "any"):
            if payload.get(cond.key) not in cond.match.any:
                return False
        elif payload.get(cond.key) != cond.match.value:
            return False
    return True


class FakeQdrant:
    """A tiny in-memory Qdrant: points are (id, vector, payload); filters behave like the real ones for our uses."""

    def __init__(self):
        self.deleted_filters = []
        self.points = {}
        self.queries = []

    def upload_points(self, collection, points, batch_size=64, wait=True):
        for point in points:
            self.points[point.id] = (point.vector, dict(point.payload))

    def delete(self, collection, points_selector):
        self.deleted_filters.append(points_selector.filter)
        self.points = {i: p for i, p in self.points.items() if not _matches(p[1], points_selector.filter)}

    def count(self, collection, count_filter=None, exact=True):
        found = [p for p in self.points.values() if count_filter is None or _matches(p[1], count_filter)]
        return models.CountResult(count=len(found))

    def set_payload(self, collection, payload, points):
        for _vector, existing in self.points.values():
            if _matches(existing, points):
                existing.update(payload)

    def query_points(self, collection, query, limit, with_payload, query_filter=None):
        self.queries.append((collection, query, limit, query_filter))
        hits = [models.ScoredPoint(id=i, version=0, score=1.0 - n / 100, payload=p)
                for n, (i, (_v, p)) in enumerate(self.points.items())
                if query_filter is None or _matches(p, query_filter)]
        return SimpleNamespace(points=hits[:limit])


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
