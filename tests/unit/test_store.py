from types import SimpleNamespace

import pytest
from qdrant_client.http.exceptions import UnexpectedResponse

from rag import store


class FakeFacetClient:
    def __init__(self, hits):
        self.hits = [SimpleNamespace(value=v, count=c) for v, c in hits]

    def facet(self, collection, key, limit, exact):
        assert key == "source" and exact
        return SimpleNamespace(hits=self.hits)


def test_indexed_sources_ignores_sources_whose_chunks_were_all_deleted():
    # Qdrant's exact facet still lists a deleted source, with a count of 0 (found by the integration tests).
    client = FakeFacetClient([("keep.pdf", 4), ("gone.pdf", 0)])
    assert store.indexed_sources(client) == {"keep.pdf": 4}


class RacingClient:
    """The collection is missing when asked about, but another process creates it before we do."""

    def __init__(self, status):
        self.status = status
        self.indexed = []

    def collection_exists(self, name):
        return False

    def create_collection(self, name, vectors_config):
        raise UnexpectedResponse(self.status, "Conflict", b"already exists", {})

    def create_payload_index(self, collection, field, schema):
        self.indexed.append(field)


def test_ensure_collection_accepts_a_collection_created_by_another_process():
    client = RacingClient(409)
    store.ensure_collection(client, lambda: 3)
    assert client.indexed == list(store.INDEXED_FIELDS)


def test_ensure_collection_still_raises_other_errors():
    with pytest.raises(UnexpectedResponse):
        store.ensure_collection(RacingClient(500), lambda: 3)
