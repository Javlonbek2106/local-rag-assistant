from types import SimpleNamespace

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
