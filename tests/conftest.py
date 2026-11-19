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
def no_warmup(monkeypatch):
    """Starting the web server would load the real models in the background; tests never talk to a real Ollama."""
    monkeypatch.setattr(config, "WARMUP", False)


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

    def ping(self):
        if getattr(self, "down", False):
            raise ConnectionError("postgresql://rag:secret-password@db/rag refused the connection")

    def status_snapshot(self):
        by_status = {}
        for v in self.versions:
            by_status[v["status"]] = by_status.get(v["status"], 0) + 1
        by_state = {}
        for d in self.docs.values():
            by_state[d["state"]] = by_state.get(d["state"], 0) + 1
        in_flight = [{"document": v["name"], "version": v["version_number"], "stage": v["status"], "attempt": 1,
                      "running_seconds": 5, "heartbeat_age_seconds": 1, "progress_unit": v.get("progress_unit"),
                      "progress_done": v.get("progress_done"), "progress_total": v.get("progress_total"),
                      "progress_age_seconds": 3 if v.get("progress_unit") else None}
                     for v in self.versions if v["status"] in db.INFLIGHT]
        errors = [{"document": v["name"], "version": v["version_number"], "status": v["status"],
                   "error": v["error_message"], "at": None}
                  for v in self.versions if v["error_message"] and v["status"] in ("FAILED", "QUEUED")]
        searchable = sum(bool(d.get("version")) for d in self.docs.values())
        return {"documents": {"total": len(self.docs), "searchable": searchable, "by_state": by_state},
                "versions": {"total": len(self.versions), "by_status": by_status},
                "in_flight_statuses": db.INFLIGHT, "queued": by_status.get("QUEUED", 0),
                "oldest_queued_age_seconds": 30 if by_status.get("QUEUED") else None,
                "in_flight": in_flight, "last_errors": errors}

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
        working = max((v for v in mine if v["status"] in db.INFLIGHT), key=lambda v: v["version_number"], default={})
        doc.update(stage=working.get("status"), progress_unit=working.get("progress_unit"),
                   progress_done=working.get("progress_done"), progress_total=working.get("progress_total"))
        doc.update(state=state, chunks=active["chunk_count"] if active else None, size_bytes=latest["file_size"],
                   version=active["version_number"] if active else None, latest_version=latest["version_number"],
                   error=latest["error_message"] if state == "failed" and failed else None,
                   update_error=latest["error_message"] if state == "done" and failed else None)

    def create_version(self, name, size_bytes, file_hash):
        doc = self.docs.setdefault(name, {"id": uuid.uuid4(), "name": name, "seconds": None, "size_bytes": size_bytes,
                                          "activation_generation": 0})
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
                   "indexed_at": None, "activated_at": None, "uploaded_generation": doc["activation_generation"]}
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
        if status not in db.INFLIGHT:  # a position means something only while a worker holds the version
            version.update(progress_unit=None, progress_done=None, progress_total=None)
        self._refresh(self.docs[name])

    def set_progress(self, name, number, unit, done, total=None):
        """Test helper: where a version being indexed is (what the worker records in the progress columns)."""
        version = next(v for v in self.versions if v["name"] == name and v["version_number"] == number)
        version.update(progress_unit=unit, progress_done=done, progress_total=total)
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

    def _view(self, version):
        """A version as rag.db returns it: with the document's current activation generation next to it."""
        return dict(version, activation_generation=self.docs[version["name"]]["activation_generation"])

    def list_versions(self, name):
        if name not in self.docs:
            return None
        found = (v for v in self.versions if v["name"] == name)
        return [self._view(v) for v in sorted(found, key=lambda v: -v["version_number"])]

    def get_version(self, name, number):
        found = next((v for v in self.versions if v["name"] == name and v["version_number"] == number), None)
        return found and self._view(found)

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
        self.docs[version["name"]]["activation_generation"] += 1  # a rollback is a decision: earlier uploads are stale
        return self.docs[version["name"]]["activation_generation"]


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

    def collection_exists(self, name):
        return not getattr(self, "missing_collection", False)

    def facet(self, collection, key, limit=10, exact=False, **kwargs):
        counts = {}
        for _vector, payload in self.points.values():
            if payload.get(key) is not None:
                counts[payload[key]] = counts.get(payload[key], 0) + 1
        ordered = sorted(counts.items(), key=lambda item: -item[1])[:limit]
        return SimpleNamespace(hits=[SimpleNamespace(value=value, count=n) for value, n in ordered])

    def scroll(self, collection, scroll_filter=None, limit=10, offset=None, with_payload=True, with_vectors=False,
               **kwargs):
        found = [(pid, payload) for pid, (_v, payload) in self.points.items()
                 if scroll_filter is None or _matches(payload, scroll_filter)]
        start = offset or 0
        page = found[start:start + limit]
        shown = [SimpleNamespace(id=pid, payload=payload if with_payload is True else
                                 {k: v for k, v in payload.items() if k in with_payload}) for pid, payload in page]
        return shown, (start + limit if start + limit < len(found) else None)

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
    """A throw-away schema in the test database, migrated once and dropped at the end.

    With RAG_TEST_HARDENED=1 the server already has the three roles (docs/postgres-hardening.md), RAG_SECRETS_DIR points
    at their passwords and RAG_TEST_DATABASE_URL is a superuser that may log in (e.g. rag_bootstrap on a server without
    the compose pg_hba.conf): the schema then belongs to rag_migrator and the suite runs through the pool of rag_app."""
    url = os.environ["RAG_TEST_DATABASE_URL"]
    schema = f"rag_test_{uuid.uuid4().hex[:8]}"
    hardened = os.getenv("RAG_TEST_HARDENED") == "1"
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}" + (" AUTHORIZATION rag_migrator" if hardened else ""))
    mp = pytest.MonkeyPatch()
    mp.setattr(config, "DATABASE_URL", url)
    mp.setattr(config, "DB_SCHEMA", schema)
    if hardened:
        parts = psycopg.conninfo.conninfo_to_dict(url)
        mp.setattr(config, "DB_HARDENED", True)
        mp.setattr(config, "DB_HOST", parts["host"])
        mp.setattr(config, "DB_PORT", int(parts["port"]))
        mp.setattr(config, "DB_NAME", parts["dbname"])
    db.pool.cache_clear()
    db.migrate()
    yield schema
    db.close()
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA {schema} CASCADE")
    mp.undo()
