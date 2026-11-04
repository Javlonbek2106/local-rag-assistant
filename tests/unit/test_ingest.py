from types import SimpleNamespace

import pytest

from rag import config, db, ingest, llm, store, worker


def chunk(pages, headings=("H",)):
    items = [SimpleNamespace(prov=[SimpleNamespace(page_no=p)]) for p in pages]
    return SimpleNamespace(meta=SimpleNamespace(doc_items=items, headings=list(headings)))


class FakeChunker:
    def __init__(self, chunks, texts):
        self._chunks, self._texts = chunks, texts

    def chunk(self, document):
        return self._chunks

    def contextualize(self, c):
        return self._texts[self._chunks.index(c)]


@pytest.mark.parametrize("name,expected", [
    ("a.pdf", True), ("A.PDF", True), ("b.docx", True), ("scan.PNG", True), ("c.md", True),
    ("old.doc", False), ("virus.exe", False), ("~$lock.docx", False), ("noext", False),
])
def test_is_supported(name, expected):
    from pathlib import Path

    assert ingest.is_supported(Path(name)) is expected


def test_build_points_keeps_pages_headings_and_stable_ids(monkeypatch):
    monkeypatch.setattr(llm, "embed", lambda texts: [[float(i)] for i in range(len(texts))])
    chunks = [chunk([3, 1, 3]), chunk([2], headings=())]
    chunker = FakeChunker(chunks, ["birinchi", "ikkinchi"])
    first = ingest.build_points(None, chunker, "C:/d/a.pdf", "a.pdf", "hash1")
    again = ingest.build_points(None, chunker, "C:/d/a.pdf", "a.pdf", "hash1")
    changed = ingest.build_points(None, chunker, "C:/d/a.pdf", "a.pdf", "hash2")

    assert [p.payload["pages"] for p in first] == [[1, 3], [2]]
    assert first[0].payload["headings"] == ["H"] and first[1].payload["headings"] == []
    assert [p.payload["chunk_index"] for p in first] == [0, 1]
    assert all(p.payload["chunks_total"] == 2 and p.payload["file_name"] == "a.pdf" for p in first)
    assert [p.id for p in first] == [p.id for p in again]  # re-indexing the same file reuses ids
    assert {p.id for p in first}.isdisjoint(p.id for p in changed)  # a changed file gets new ids
    assert first[0].id != first[1].id


def test_a_document_without_text_is_unreadable_not_a_temporary_failure():
    chunker = FakeChunker([chunk([1])], ["   "])
    with pytest.raises(ingest.UnreadableDocument, match="no text"):
        ingest.build_points(None, chunker, "s", "a.png", "h")


def test_purge_orphans_removes_only_entries_without_a_document(monkeypatch):
    deleted = []
    monkeypatch.setattr(store, "indexed_sources", lambda client: {"id-1": 3, "id-2": 5, "id-gone": 2})
    monkeypatch.setattr(store, "delete_source", lambda client, source: deleted.append(source))
    assert worker.purge_orphans("client", {"id-1", "id-2"}) == 1
    assert deleted == ["id-gone"]


def test_purge_orphans_refuses_to_wipe_the_index_when_no_document_is_known(monkeypatch):
    monkeypatch.setattr(store, "indexed_sources", lambda client: {"id-1": 3})
    monkeypatch.setattr(store, "delete_source", lambda client, source: pytest.fail("must not delete"))
    assert worker.purge_orphans("client", set()) == 0


def test_queue_file_registers_stores_and_enqueues_in_that_order(tmp_path, storage_dir, repo):
    temp = tmp_path / "upload.tmp"
    temp.write_bytes(b"matn")
    doc_id = ingest.queue_file("a.txt", temp, 4, repo)
    assert repo.docs["a.txt"]["id"] == doc_id and repo.docs["a.txt"]["state"] == "queued"
    assert (storage_dir / str(doc_id) / "a.txt").read_bytes() == b"matn"
    assert not temp.exists()


def test_queue_file_refuses_a_document_that_is_being_indexed(tmp_path, storage_dir, repo):
    ingest.queue_file("a.txt", _temp(tmp_path, "one"), 3, repo)
    repo.docs["a.txt"]["state"] = "processing"
    with pytest.raises(db.Busy):
        ingest.queue_file("a.txt", _temp(tmp_path, "two"), 3, repo)


def _temp(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"x")
    return path


def test_index_file_uses_the_given_source_instead_of_the_path(tmp_path, monkeypatch):
    path = tmp_path / "a.txt"
    path.write_text("matn", encoding="utf-8")
    seen = []

    def already_indexed(client, source, file_hash):
        seen.append(source)
        return True

    monkeypatch.setattr(store, "is_indexed", already_indexed)
    assert ingest.index_file(None, None, None, path, source="doc-123") is None
    assert seen == ["doc-123"]


class FakeCountClient:
    def __init__(self, stored, chunks_total):
        self.stored, self.chunks_total = stored, chunks_total

    def scroll(self, collection, scroll_filter, limit, with_payload):
        points = [SimpleNamespace(payload={"chunks_total": self.chunks_total})] if self.stored else []
        return points, None

    def count(self, collection, count_filter, exact):
        return SimpleNamespace(count=self.stored)


def test_is_indexed_requires_every_chunk_of_that_exact_file_version():
    assert store.is_indexed(FakeCountClient(5, 5), "s", "h") is True
    assert store.is_indexed(FakeCountClient(3, 5), "s", "h") is False  # an interrupted upload
    assert store.is_indexed(FakeCountClient(0, 0), "s", "h") is False


def test_source_filter_matches_source_and_optionally_hash():
    assert [c.key for c in store.source_filter("s").must] == ["source"]
    assert [c.key for c in store.source_filter("s", "h").must] == ["source", "file_hash"]
    assert config.COLLECTION  # the filter is applied to the configured collection by the callers
