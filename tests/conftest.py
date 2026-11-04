import os

import pytest
from fastapi.testclient import TestClient

from rag import config, store
from rag.api import create_app


def pytest_collection_modifyitems(config, items):
    """Integration and e2e tests need real services; they only run when explicitly switched on."""
    for marker, switch in (("integration", "RAG_INTEGRATION"), ("e2e", "RAG_E2E")):
        if os.getenv(switch) == "1":
            continue
        skip = pytest.mark.skip(reason=f"set {switch}=1 to run")
        for item in items:
            if marker in item.keywords:
                item.add_marker(skip)


class FakeWorker:
    """Stands in for IndexWorker so API tests need no models and no threads."""

    def __init__(self):
        self.submitted = []
        self.processing = set()
        self.forgotten = []
        self.status = {}
        self.error = None
        self.started = False

    def start(self):
        self.started = True

    def submit(self, name):
        self.submitted.append(name)

    def is_processing(self, name):
        return name in self.processing

    def forget(self, name):
        self.forgotten.append(name)

    def queued(self):
        return len(self.submitted)

    def snapshot(self):
        return self.status


class FakeQdrant:
    def __init__(self):
        self.deleted_filters = []

    def delete(self, collection, points_selector):
        self.deleted_filters.append(points_selector.filter)


@pytest.fixture
def docs_dir(tmp_path, monkeypatch):
    path = tmp_path / "documents"
    monkeypatch.setattr(config, "DOCS_DIR", path)
    return path


@pytest.fixture
def worker():
    return FakeWorker()


@pytest.fixture
def qdrant(monkeypatch):
    fake = FakeQdrant()
    monkeypatch.setattr(store, "get_client", lambda: fake)
    return fake


@pytest.fixture
def client(docs_dir, worker, qdrant):
    with TestClient(create_app(worker), base_url=f"http://127.0.0.1:{config.PORT}") as test_client:
        yield test_client
