"""Everything that talks to Qdrant. A document is identified by its `source` (the file path)."""
import functools
import logging

from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse

from rag import config

log = logging.getLogger(__name__)
INDEXED_FIELDS = ("source", "file_hash", "file_name")


@functools.lru_cache(maxsize=1)
def get_client():
    return QdrantClient(url=config.QDRANT_URL)


def ensure_collection(client, vector_size):
    """Create the collection if missing. `vector_size` is a callable, called only when a probe is needed."""
    if not client.collection_exists(config.COLLECTION):
        try:
            client.create_collection(
                config.COLLECTION,
                vectors_config=models.VectorParams(size=vector_size(), distance=models.Distance.COSINE),
            )
            log.info("created collection %s", config.COLLECTION)
        except UnexpectedResponse as error:
            if error.status_code != 409:  # 409: the API and the worker started together and the other one won
                raise
    # Without these indexes every per-file lookup scans the whole collection. Idempotent, also fixes old collections.
    for field in INDEXED_FIELDS:
        client.create_payload_index(config.COLLECTION, field, models.PayloadSchemaType.KEYWORD)


def source_filter(source, file_hash=None):
    conditions = [models.FieldCondition(key="source", match=models.MatchValue(value=source))]
    if file_hash:
        conditions.append(models.FieldCondition(key="file_hash", match=models.MatchValue(value=file_hash)))
    return models.Filter(must=conditions)


def count_chunks(client, source):
    return client.count(config.COLLECTION, count_filter=source_filter(source), exact=True).count


def is_indexed(client, source, file_hash):
    """True only if every chunk of this exact file version is stored."""
    match = source_filter(source, file_hash)
    points, _ = client.scroll(config.COLLECTION, scroll_filter=match, limit=1, with_payload=["chunks_total"])
    if not points:
        return False
    return client.count(config.COLLECTION, count_filter=match, exact=True).count == points[0].payload["chunks_total"]


def delete_source(client, source):
    client.delete(config.COLLECTION, points_selector=models.FilterSelector(filter=source_filter(source)))


def indexed_sources(client):
    """Map of source path -> chunk count for everything in the index."""
    response = client.facet(config.COLLECTION, key="source", limit=100_000, exact=True)
    # An exact facet keeps listing values whose points were all deleted, with a count of 0.
    return {hit.value: hit.count for hit in response.hits if hit.count > 0}
