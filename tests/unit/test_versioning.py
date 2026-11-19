import hashlib
import uuid
from types import SimpleNamespace

import pytest

from rag import ingest, llm, store, versioning
from tests.conftest import FakeQdrant, FakeRepo

DOC, VERSION = uuid.uuid4(), uuid.uuid4()


def chunks(client, version_id=VERSION, document_id=DOC, file_hash="h", total=3, count=None):
    for i in range(total if count is None else count):
        client.points[f"{version_id}-{i}"] = ([0.1], {"document_id": str(document_id), "version_id": str(version_id),
                                                      "file_hash": file_hash, "chunks_total": total, "source": "s"})


def test_a_complete_index_passes():
    client = FakeQdrant()
    chunks(client)
    versioning.verify(client, DOC, VERSION, "h", 3)


@pytest.mark.parametrize("kwargs, expected, message", [
    ({"count": 2}, 3, "2 ta"),                      # a chunk never arrived
    ({"total": 4, "count": 4}, 3, "4 ta"),          # a leftover chunk from an earlier attempt
    ({"document_id": uuid.uuid4()}, 3, "boshqa"),   # the chunks belong to another document
    ({"file_hash": "other"}, 3, "boshqa"),          # they were made from another file
    ({"total": 5, "count": 3}, 3, "boshqa"),        # they were cut for a different chunk total
])
def test_every_kind_of_damage_is_refused(kwargs, expected, message):
    client = FakeQdrant()
    chunks(client, **kwargs)
    with pytest.raises(versioning.IndexIncomplete, match=message):
        versioning.verify(client, DOC, VERSION, "h", expected)


def test_chunks_of_another_version_do_not_count():
    client = FakeQdrant()
    chunks(client, version_id=uuid.uuid4())
    with pytest.raises(versioning.IndexIncomplete):
        versioning.verify(client, DOC, VERSION, "h", 3)


# ---------- rollback ----------

def two_versions(repo, qdrant, old_chunks=3):
    repo.create_version("a.txt", 1, "h1")
    repo.create_version("a.txt", 2, "h2")
    old, new = repo.versions
    repo.set_status("a.txt", 1, "READY", chunks=old_chunks, active=True)
    repo.set_status("a.txt", 2, "READY", chunks=1, active=True)
    chunks(qdrant, old["id"], old["document_id"], "h1", old_chunks)
    chunks(qdrant, new["id"], new["document_id"], "h2", 1)
    return old, new


def test_rollback_switches_without_changing_the_index():
    repo, qdrant = FakeRepo(), FakeQdrant()
    old, new = two_versions(repo, qdrant)
    restored = versioning.rollback(repo, qdrant, "a.txt", 1)
    assert restored["active"] and repo.active_version_ids() == [str(old["id"])]
    assert repo.get_version("a.txt", 2)["status"] == "SUPERSEDED"
    assert len(qdrant.points) == 4 and qdrant.deleted_filters == []


@pytest.mark.parametrize("damage", ["missing", "wrong_document", "wrong_hash"])
def test_rollback_is_refused_and_changes_nothing_when_the_old_vectors_are_not_intact(damage):
    repo, qdrant = FakeRepo(), FakeQdrant()
    old, new = two_versions(repo, qdrant)
    key = f"{old['id']}-0"
    if damage == "missing":
        del qdrant.points[key]
    elif damage == "wrong_document":
        qdrant.points[key][1]["document_id"] = str(uuid.uuid4())
    else:
        qdrant.points[key][1]["file_hash"] = "tampered"
    with pytest.raises(versioning.RollbackError) as error:
        versioning.rollback(repo, qdrant, "a.txt", 1)
    assert error.value.status == 409 and "to'liq emas" in str(error.value)
    assert repo.active_version_ids() == [str(new["id"])]


def test_rollback_refuses_unknown_active_and_never_active_versions():
    repo, qdrant = FakeRepo(), FakeQdrant()
    two_versions(repo, qdrant)
    repo.create_version("a.txt", 3, "h3")  # uploaded, never active
    for number, status in ((9, 404), (2, 409), (3, 409)):
        with pytest.raises(versioning.RollbackError) as error:
            versioning.rollback(repo, qdrant, "a.txt", number)
        assert error.value.status == status


def test_rollback_does_not_embed_anything(monkeypatch):
    monkeypatch.setattr(llm, "embed", lambda texts: pytest.fail("a rollback must not re-embed"))
    repo, qdrant = FakeRepo(), FakeQdrant()
    two_versions(repo, qdrant)
    versioning.rollback(repo, qdrant, "a.txt", 1)


# ---------- the backfill of documents indexed before versioning ----------

def test_backfill_tags_the_chunks_and_hashes_the_stored_file(storage_dir):
    repo, qdrant = FakeRepo(), FakeQdrant()
    repo.create_version("a.txt", 5, None)
    version = repo.versions[0]
    version["file_path"] = f"{version['document_id']}/a.txt"  # where a pre-versioning file lives
    legacy = storage_dir / str(version["document_id"]) / "a.txt"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"hello")
    qdrant.points["old"] = ([0.1], {"source": str(version["document_id"])})
    assert versioning.backfill_legacy(repo, qdrant) == 1
    assert qdrant.points["old"][1]["version_id"] == str(version["id"])
    assert version["file_hash"] == hashlib.sha256(b"hello").hexdigest()
    assert versioning.backfill_legacy(repo, qdrant) == 0  # nothing left to do


def test_backfill_survives_a_missing_file(storage_dir):
    repo, qdrant = FakeRepo(), FakeQdrant()
    repo.create_version("a.txt", 5, None)
    assert versioning.backfill_legacy(repo, qdrant) == 1 and repo.versions[0]["file_hash"] is None


# ---------- indexing one version ----------

def fake_pipeline(monkeypatch, texts):
    monkeypatch.setattr(ingest, "extract_items", lambda converter, chunker, path: [(t, [1], []) for t in texts])
    monkeypatch.setattr(llm, "embed", lambda batch: [[0.1] for _ in batch])


def index(qdrant, version_id=VERSION, stage=lambda s: None, file_hash="h"):
    return ingest.index_version(qdrant, None, None, None, document_id=DOC, version_id=version_id, file_name="a.txt",
                                file_hash=file_hash, stage=stage)


def test_indexing_a_version_writes_its_chunks_with_both_ids_and_reports_the_stages(monkeypatch):
    fake_pipeline(monkeypatch, ["bir", "ikki"])
    qdrant, seen = FakeQdrant(), []
    assert index(qdrant, stage=seen.append) == 2
    assert seen == ["EMBEDDING", "INDEXING"]
    payloads = [p for _v, p in qdrant.points.values()]
    assert {(p["document_id"], p["version_id"], p["source"], p["file_hash"], p["chunks_total"])
            for p in payloads} == {(str(DOC), str(VERSION), str(DOC), "h", 2)}
    assert sorted(p["chunk_index"] for p in payloads) == [0, 1]
    versioning.verify(qdrant, DOC, VERSION, "h", 2)


def test_point_ids_are_stable_per_version_and_chunk_and_differ_between_versions():
    items = [("a", [1], []), ("b", [1], [])]
    first = ingest._points(items, [[0], [0]], str(DOC), "a.txt", "h", DOC, VERSION)
    again = ingest._points(items, [[0], [0]], str(DOC), "a.txt", "h", DOC, VERSION)
    other = ingest._points(items, [[0], [0]], str(DOC), "a.txt", "h", DOC, uuid.uuid4())
    assert [p.id for p in first] == [p.id for p in again]
    assert len({p.id for p in first} | {p.id for p in other}) == 4


def test_a_retry_writes_the_same_points_again_without_duplicates(monkeypatch):
    fake_pipeline(monkeypatch, ["bir", "ikki", "uch"])
    qdrant = FakeQdrant()
    index(qdrant)
    index(qdrant)  # the first attempt died after uploading; the retry does everything again
    assert store.count_version(qdrant, VERSION) == 3


def test_a_retry_that_yields_fewer_chunks_leaves_no_leftovers(monkeypatch):
    qdrant = FakeQdrant()
    fake_pipeline(monkeypatch, ["bir", "ikki", "uch", "to'rt"])
    index(qdrant)
    fake_pipeline(monkeypatch, ["bir", "ikki"])  # e.g. OCR found less this time
    index(qdrant)
    assert store.count_version(qdrant, VERSION) == 2
    versioning.verify(qdrant, DOC, VERSION, "h", 2)


def test_indexing_one_version_never_touches_another_versions_chunks(monkeypatch):
    qdrant = FakeQdrant()
    other = uuid.uuid4()
    fake_pipeline(monkeypatch, ["eski"])
    index(qdrant, version_id=other)
    fake_pipeline(monkeypatch, ["yangi 1", "yangi 2"])
    index(qdrant)
    assert store.count_version(qdrant, other) == 1 and store.count_version(qdrant, VERSION) == 2


def test_a_stage_callback_that_raises_stops_before_anything_is_written(monkeypatch):
    fake_pipeline(monkeypatch, ["bir"])
    qdrant = FakeQdrant()

    def lost(status):
        if status == "INDEXING":
            raise RuntimeError("claim lost")

    with pytest.raises(RuntimeError):
        index(qdrant, stage=lost)
    assert qdrant.points == {}


def test_a_document_without_text_is_unreadable_and_writes_nothing(monkeypatch):
    fake_pipeline(monkeypatch, ["", "  "])
    qdrant = FakeQdrant()
    with pytest.raises(ingest.UnreadableDocument):
        index(qdrant)
    assert qdrant.points == {}


def test_the_evaluation_path_is_unchanged_and_writes_unversioned_chunks(monkeypatch, tmp_path):
    fake_pipeline(monkeypatch, ["bir"])
    path = tmp_path / "a.txt"
    path.write_text("matn", encoding="utf-8")
    qdrant = SimpleNamespace(uploaded=[])
    qdrant.upload_points = lambda collection, points, batch_size, wait: qdrant.uploaded.extend(points)
    qdrant.delete = lambda *args, **kwargs: None
    monkeypatch.setattr(store, "is_indexed", lambda client, source, file_hash: False)
    assert ingest.index_file(qdrant, None, None, path) == 1
    assert "version_id" not in qdrant.uploaded[0].payload and qdrant.uploaded[0].payload["source"] == str(path)
