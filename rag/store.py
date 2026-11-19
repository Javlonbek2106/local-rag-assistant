"""Everything that talks to Qdrant, a derived index that can be rebuilt from PostgreSQL and the stored files.

Every chunk carries `document_id` and `version_id` (PostgreSQL decides which version is active, see rag.db);
`source` stays the document id for versioned chunks (the file path in evaluation indexes), so everything that
works per document keeps working. Searches always filter on the active version ids.
"""
import functools
import logging

from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse

from rag import config

log = logging.getLogger(__name__)
INDEXED_FIELDS = ("source", "file_hash", "file_name", "document_id", "version_id")


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


def version_filter(version_id, document_id=None, file_hash=None, chunks_total=None):
    """Chunks of one version; the optional arguments narrow it to chunks that also carry exactly these values."""
    values = {"version_id": str(version_id), "document_id": document_id and str(document_id),
              "file_hash": file_hash, "chunks_total": chunks_total}
    return models.Filter(must=[models.FieldCondition(key=key, match=models.MatchValue(value=value))
                               for key, value in values.items() if value is not None])


def active_filter(version_ids):
    """Only chunks of these versions. The caller must handle an empty list itself (it matches nothing)."""
    return models.Filter(must=[models.FieldCondition(key="version_id", match=models.MatchAny(any=list(version_ids)))])


def count_version(client, version_id, **narrow):
    return client.count(config.COLLECTION, count_filter=version_filter(version_id, **narrow), exact=True).count


def delete_version(client, version_id):
    """Drop a version's own chunks (a retry starts clean; nothing else is touched)."""
    client.delete(config.COLLECTION, points_selector=models.FilterSelector(filter=version_filter(version_id)))


def tag_legacy(client, document_id, version_id):
    """Give chunks indexed before versioning their ids. Touches only chunks that have no version_id yet, so it is safe
    to repeat and never overwrites a versioned chunk."""
    untagged = models.IsEmptyCondition(is_empty=models.PayloadField(key="version_id"))
    only_this_document = source_filter(str(document_id))
    client.set_payload(
        config.COLLECTION,
        payload={"document_id": str(document_id), "version_id": str(version_id)},
        points=models.Filter(must=[*only_this_document.must, untagged]),
    )


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
