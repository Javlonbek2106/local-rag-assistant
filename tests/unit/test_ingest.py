from types import SimpleNamespace

import pytest

from rag import config, ingest, llm, store


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


def test_build_points_rejects_documents_without_text():
    chunker = FakeChunker([chunk([1])], ["   "])
    with pytest.raises(ValueError, match="no text"):
        ingest.build_points(None, chunker, "s", "a.png", "h")


def test_import_copies_supported_files_and_reports_the_rest(tmp_path, docs_dir):
    source = tmp_path / "incoming"
    (source / "sub").mkdir(parents=True)
    (source / "a.txt").write_bytes(b"a")
    (source / "sub" / "b.pdf").write_bytes(b"b")
    (source / "sub" / "a.txt").write_bytes(b"duplicate name")
    (source / "old.doc").write_bytes(b"x")
    paths, problems = ingest.import_into_documents(source)

    assert sorted(p.name for p in paths) == ["a.txt", "b.pdf"]
    assert (docs_dir / "a.txt").read_bytes() == b"a"
    reasons = {p.name: reason for p, reason in problems}
    assert "unsupported" in reasons["old.doc"] and "same name" in reasons["a.txt"]


def test_import_does_not_copy_a_file_onto_itself(docs_dir):
    docs_dir.mkdir(parents=True)
    (docs_dir / "a.txt").write_bytes(b"a")
    paths, problems = ingest.import_into_documents(docs_dir)
    assert paths == [docs_dir / "a.txt"] and problems == []


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
