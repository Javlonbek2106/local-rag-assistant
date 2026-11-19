"""Backup and restore against a REAL PostgreSQL and a REAL Qdrant, in throw-away Docker containers on free ports
(tests/stack.py), never the running system. Vectors are small synthetic ones, so no Ollama or Docling is needed.

Needs RAG_INTEGRATION=1 and a working Docker; skipped otherwise.
"""
import hashlib
import time
import uuid
from types import SimpleNamespace

import pytest
from qdrant_client import models

from rag import backup, config, db, ingest, reconcile, storage, store, versioning
from rag.backup import BackupError, BackupInconsistent, PgTools, RestoreRefused
from rag.target import Target
from tests import stack as stack_tools

pytestmark = pytest.mark.integration
SOURCE_COLLECTION = "src_docs"
DIM = 4


@pytest.fixture(scope="module")
def stack():
    if not stack_tools.docker_available():
        pytest.skip("Docker is not available")
    running = stack_tools.Stack("br").start()
    yield running
    running.stop()


def vector(seed):
    return [float(seed + 1), 1.0, float(seed % 3), 0.5]


class Source:
    """The source system: three documents with history, real rows, real files, real vectors."""

    def __init__(self, stack, storage_dir):
        self.stack = stack
        self.target = Target(stack.database_url, stack.qdrant_url, SOURCE_COLLECTION, storage_dir)
        self.client = self.target.qdrant()
        self.pg = PgTools(container=stack.pg_container)

    def populate(self):
        self.client.create_collection(SOURCE_COLLECTION, vectors_config=models.VectorParams(
            size=DIM, distance=models.Distance.COSINE))
        for field in store.INDEXED_FIELDS:
            self.client.create_payload_index(SOURCE_COLLECTION, field, models.PayloadSchemaType.KEYWORD)
        db.migrate()
        self.publish("alpha.txt", "alpha, first version", chunks=2, seed=0)
        self.alpha_v2 = self.publish("alpha.txt", "alpha, second version", chunks=3, seed=10)
        self.publish("beta.txt", "beta text", chunks=2, seed=20)
        failed = db.create_version("gamma.txt", 5, hashlib.sha256(b"gamma").hexdigest())
        path = storage.resolve(failed["file_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"gamma")
        db.enqueue(failed["id"])
        job = db.claim_next("w1")
        db.fail(job["id"], "w1", "no text could be extracted", retry=False)
        # a rollback and a roll-forward: the activation generation is 2 and has to survive a restore
        first = db.get_version("alpha.txt", 1)
        db.switch_active(first["id"])
        db.switch_active(self.alpha_v2["id"])

    def publish(self, name, content, chunks, seed):
        data = content.encode()
        file_hash = hashlib.sha256(data).hexdigest()
        created = db.create_version(name, len(data), file_hash)
        path = storage.resolve(created["file_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        db.enqueue(created["id"])
        job = db.claim_next("w1")
        while job["id"] != created["id"]:
            job = db.claim_next("w1")
        items = [(f"{name} chunk {i}", [1], []) for i in range(chunks)]
        points = ingest._points(items, [vector(seed + i) for i in range(chunks)], str(created["document_id"]), name,
                                file_hash, created["document_id"], created["id"])
        self.client.upload_points(SOURCE_COLLECTION, points=points, wait=True)
        assert db.set_stage(job["id"], "w1", "VERIFYING") and db.record_verified(job["id"], "w1", chunks)
        assert db.activate(job["id"], "w1", chunks) == "activated"
        return created


@pytest.fixture(scope="module")
def source(stack, tmp_path_factory):
    storage_dir = tmp_path_factory.mktemp("source-storage")
    mp = pytest.MonkeyPatch()
    mp.setattr(config, "DATABASE_URL", stack.database_url)
    mp.setattr(config, "DB_SCHEMA", None)
    mp.setattr(config, "STORAGE_DIR", storage_dir)
    db.pool.cache_clear()
    system = Source(stack, storage_dir)
    system.populate()
    db.close()
    mp.undo()
    yield system
    system.client.delete_collection(SOURCE_COLLECTION)


def new_target(source, tmp_path):
    n = uuid.uuid4().hex[:8]
    url = source.stack.create_database(f"restore_{n}")
    return Target(url, source.stack.qdrant_url, f"restored_{n}", tmp_path / "restored-storage")


def table_rows(target):
    with target.connect() as conn:
        conn.read_only = True
        return {t: sorted(map(str, (tuple(r.values()) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1"))))
                for t in ("documents", "document_versions", "schema_migrations")}


def all_points(client, collection):
    found, offset = {}, None
    while True:
        points, offset = client.scroll(collection, limit=100, offset=offset, with_payload=True, with_vectors=True)
        found.update({str(p.id): ([round(x, 6) for x in p.vector], p.payload) for p in points})
        if offset is None:
            return found


def stored_files(root):
    return {relative: hashlib.sha256(path.read_bytes()).hexdigest() for relative, path in backup.storage_files(root)}


def make_backup(source, tmp_path):
    return backup.backup(source.target, tmp_path / "backups", source.pg)


# ---------- taking a backup ----------

def test_a_backup_is_a_complete_verified_folder_with_a_full_manifest(source, tmp_path):
    folder = make_backup(source, tmp_path)
    assert folder.name.startswith("rag-backup-") and not folder.name.endswith((".partial", ".failed"))
    manifest = backup.read_manifest(folder)
    assert manifest["status"] == "complete" and manifest["consistent"] is True
    assert {a["name"] for a in manifest["artifacts"]} == {"postgres.dump", "qdrant.snapshot"}
    assert manifest["storage"]["file_count"] == 4 and len(manifest["storage"]["files"]) == 4
    assert manifest["qdrant"]["collection"] == SOURCE_COLLECTION and manifest["qdrant"]["points_count"] == 7
    assert manifest["qdrant"]["vector_size"] == DIM and manifest["qdrant"]["distance"] == "Cosine"
    # every migration the code has, in order (the list grows with each new migration file)
    assert manifest["postgres"]["migrations"] == sorted(p.stem for p in db.MIGRATIONS.glob("*.sql"))
    assert manifest["postgres"]["server_version"].startswith("17")
    assert backup.verify(folder) == []
    assert not (folder / ".manifest.json.tmp").exists()
    text = (folder / "manifest.json").read_text(encoding="utf-8")
    assert "rag:rag" not in text and "password" not in text.lower()  # no credentials anywhere in the manifest


def test_taking_a_backup_changes_nothing_and_leaves_no_snapshot_on_the_server(source, tmp_path):
    rows, points, files = table_rows(source.target), all_points(source.client, SOURCE_COLLECTION), stored_files(
        source.target.storage_dir)
    make_backup(source, tmp_path)
    assert table_rows(source.target) == rows and all_points(source.client, SOURCE_COLLECTION) == points
    assert stored_files(source.target.storage_dir) == files
    assert source.client.list_snapshots(SOURCE_COLLECTION) == []  # the server's copy was removed again


def test_a_backup_can_be_taken_again_and_again(source, tmp_path):
    first, second = make_backup(source, tmp_path), make_backup(source, tmp_path)
    assert first != second and backup.verify(first) == [] and backup.verify(second) == []
    assert len([p for p in (tmp_path / "backups").iterdir()]) == 2


def test_a_backup_that_fails_is_marked_failed_and_never_called_complete(source, tmp_path):
    with pytest.raises(BackupError, match="pg_dump failed"):
        backup.backup(source.target, tmp_path / "backups", PgTools(container="no-such-container-anywhere"))
    (folder,) = list((tmp_path / "backups").iterdir())
    assert folder.name.endswith(".failed") and backup.read_manifest(folder)["status"] == "failed"
    assert backup.verify(folder) != []
    with pytest.raises(RestoreRefused):
        backup.restore(folder, Target("postgresql://x/y", "http://q", "c", tmp_path / "s"), source.pg)
    assert source.client.list_snapshots(SOURCE_COLLECTION) == []  # it never got as far as the snapshot


def test_a_backup_during_which_the_data_changed_is_marked_inconsistent(source, tmp_path):
    extra = models.PointStruct(id=str(uuid.uuid4()), vector=vector(99), payload={"note": "added during the backup"})

    def change_something(phase):
        if phase == "postgres":
            source.client.upload_points(SOURCE_COLLECTION, points=[extra], wait=True)

    try:
        with pytest.raises(BackupInconsistent):
            backup.backup(source.target, tmp_path / "backups", source.pg, on_phase=change_something)
        (folder,) = list((tmp_path / "backups").iterdir())
        assert folder.name.endswith(".inconsistent") and backup.read_manifest(folder)["status"] == "inconsistent"
        assert backup.verify(folder) != []
    finally:
        source.client.delete(SOURCE_COLLECTION, points_selector=models.PointIdsList(points=[extra.id]))


def test_an_inconsistent_backup_can_be_kept_only_on_purpose_and_is_then_flagged(source, tmp_path):
    extra = models.PointStruct(id=str(uuid.uuid4()), vector=vector(98), payload={"note": "added during the backup"})

    def change_something(phase):
        if phase == "storage":
            source.client.upload_points(SOURCE_COLLECTION, points=[extra], wait=True)

    try:
        folder = backup.backup(source.target, tmp_path / "backups", source.pg, allow_inconsistent=True,
                               on_phase=change_something)
    finally:
        source.client.delete(SOURCE_COLLECTION, points_selector=models.PointIdsList(points=[extra.id]))
    assert backup.read_manifest(folder)["consistent"] is False
    assert backup.verify(folder) != [] and backup.verify(folder, allow_inconsistent=True) == []


def test_a_backup_does_not_start_when_the_source_is_not_there(source, tmp_path):
    missing = Target(source.stack.database_url, source.stack.qdrant_url, "no_such_collection",
                     source.target.storage_dir)
    with pytest.raises(BackupError, match="does not exist"):
        backup.backup(missing, tmp_path / "backups", source.pg)
    assert not (tmp_path / "backups").exists() or list((tmp_path / "backups").iterdir()) == []


def test_waiting_for_an_idle_system_gives_up_when_work_never_stops(source, tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "idle", lambda target: False)
    monkeypatch.setattr(backup, "time", SimpleNamespace(monotonic=time.monotonic, sleep=lambda seconds: None))
    with pytest.raises(BackupError, match="still queued or being indexed"):
        backup.backup(source.target, tmp_path / "backups", source.pg, wait_idle=0.0001)
    assert not (tmp_path / "backups").exists() or list((tmp_path / "backups").iterdir()) == []


# ---------- restoring ----------

def test_a_restore_into_an_empty_environment_reproduces_everything(source, tmp_path):
    folder = make_backup(source, tmp_path)
    target = new_target(source, tmp_path)
    result = backup.restore(folder, target, source.pg)

    assert result["ok"] is True and result["reconcile"]["ok"] is True and result["reconcile"]["errors"] == 0
    assert set(result["seconds"]) >= {"verify", "postgres", "storage", "qdrant", "reconcile", "total"}
    # PostgreSQL: every row of every table, including ids, statuses, the active pointers and the generations
    assert table_rows(target) == table_rows(source.target)
    # the original files, byte for byte
    assert stored_files(target.storage_dir) == stored_files(source.target.storage_dir)
    # Qdrant: every point with its vector and payload
    assert all_points(target.qdrant(), target.collection) == all_points(source.client, SOURCE_COLLECTION)
    # and a clean reconciliation of the restored system, including the versions kept for rollback
    assert reconcile.run(target, reconcile.Options(include_superseded=True)).exit_code == 0


def test_the_restored_documents_are_searchable_exactly_like_the_originals(source, tmp_path):
    target = new_target(source, tmp_path)
    backup.restore(make_backup(source, tmp_path), target, source.pg)
    restored = target.qdrant()
    with target.connect() as conn:
        active = [r["active_version_id"] for r in conn.execute(
            "SELECT active_version_id::text AS active_version_id FROM documents WHERE active_version_id IS NOT NULL")]
    for seed in (0, 10, 20, 31):
        asked = dict(query=vector(seed), limit=5, with_payload=True, query_filter=store.active_filter(active))
        original = source.client.query_points(SOURCE_COLLECTION, **asked).points
        again = restored.query_points(target.collection, **asked).points
        assert [(p.id, round(p.score, 5)) for p in again] == [(p.id, round(p.score, 5)) for p in original] != []
    assert reconcile.run(target).exit_code == 0


def test_version_history_the_generation_and_rollback_survive_a_restore(source, tmp_path, monkeypatch):
    target = new_target(source, tmp_path)
    backup.restore(make_backup(source, tmp_path), target, source.pg)
    monkeypatch.setattr(config, "DATABASE_URL", target.database_url)
    monkeypatch.setattr(config, "DB_SCHEMA", None)
    monkeypatch.setattr(config, "COLLECTION", target.collection)
    db.pool.cache_clear()
    try:
        document = db.get_document("alpha.txt")
        assert document["version"] == 2 and document["activation_generation"] == 2  # history and generation intact
        assert [v["status"] for v in db.list_versions("alpha.txt")] == ["READY", "SUPERSEDED"]
        # a rollback needs the old version's vectors and files: it works on the restored system, nothing re-embedded
        restored = versioning.rollback(db, target.qdrant(), "alpha.txt", 1)
        assert restored["active"] and db.get_document("alpha.txt")["version"] == 1
        assert db.get_document("alpha.txt")["activation_generation"] == 3
        assert reconcile.run(target, reconcile.Options(include_superseded=True)).exit_code == 0
    finally:
        db.close()


def test_restoring_never_goes_into_something_that_is_not_empty(source, tmp_path):
    folder = make_backup(source, tmp_path)
    target = new_target(source, tmp_path)
    backup.restore(folder, target, source.pg)
    before = (table_rows(target), all_points(target.qdrant(), target.collection), stored_files(target.storage_dir))
    with pytest.raises(RestoreRefused) as refusal:  # the very same target again: database, collection and storage
        backup.restore(folder, target, source.pg)
    message = str(refusal.value)
    assert "not empty" in message and "already exists" in message and "storage folder" in message
    after = (table_rows(target), all_points(target.qdrant(), target.collection), stored_files(target.storage_dir))
    assert after == before


def test_the_running_system_cannot_be_overwritten_by_a_restore(source, tmp_path):
    """Aiming a restore at the SOURCE itself (a database with tables, an existing collection, a filled storage)."""
    folder = make_backup(source, tmp_path)
    rows, points = table_rows(source.target), all_points(source.client, SOURCE_COLLECTION)
    with pytest.raises(RestoreRefused):
        backup.restore(folder, source.target, source.pg)
    assert table_rows(source.target) == rows and all_points(source.client, SOURCE_COLLECTION) == points


def test_a_tampered_backup_is_refused_and_the_target_stays_empty(source, tmp_path):
    folder = make_backup(source, tmp_path)
    snapshot = folder / "qdrant.snapshot"
    data = bytearray(snapshot.read_bytes())
    data[len(data) // 2] ^= 0xFF  # one flipped byte
    snapshot.write_bytes(bytes(data))
    target = new_target(source, tmp_path)
    with pytest.raises(RestoreRefused, match="does not match its SHA-256"):
        backup.restore(folder, target, source.pg)
    with target.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM information_schema.tables WHERE table_schema = 'public'"
                            ).fetchone()["n"] == 0
    assert not target.qdrant().collection_exists(target.collection) and not target.storage_dir.exists()


def test_a_restore_that_fails_half_way_is_an_error_not_a_success(source, tmp_path):
    folder = make_backup(source, tmp_path)
    target = new_target(source, tmp_path)
    with pytest.raises(BackupError):
        backup.restore(folder, target, PgTools(container="no-such-container-anywhere"))
    assert not target.qdrant().collection_exists(target.collection)  # it stopped before the later steps


# ---------- the commands ----------

def test_the_backup_and_restore_commands_end_to_end(source, tmp_path, monkeypatch, capsys):
    for name, value in (("DATABASE_URL", source.target.database_url), ("QDRANT_URL", source.target.qdrant_url),
                        ("COLLECTION", SOURCE_COLLECTION), ("STORAGE_DIR", source.target.storage_dir),
                        ("DB_SCHEMA", None)):
        monkeypatch.setattr(config, name, value)
    assert backup.main(["--to", str(tmp_path / "backups"), "--pg-container", source.stack.pg_container, "--json"]) == 0
    (folder,) = list((tmp_path / "backups").iterdir())
    assert '"status": "complete"' in capsys.readouterr().out.replace('"status":"complete"', '"status": "complete"')
    assert backup.main(["--verify", str(folder)]) == 0

    target = new_target(source, tmp_path)
    args = [str(folder), "--database-url", target.database_url, "--qdrant-url", target.qdrant_url,
            "--storage", str(target.storage_dir), "--collection", target.collection, "--pg-container",
            source.stack.pg_container]
    assert backup.restore_main(args) == 0
    assert backup.restore_main(args) == 1  # the second time the target is not empty any more
    assert reconcile.run(target).exit_code == 0


def test_a_command_line_backup_that_cannot_run_exits_with_1(source, tmp_path, monkeypatch, capsys):
    for name, value in (("DATABASE_URL", source.target.database_url), ("QDRANT_URL", source.target.qdrant_url),
                        ("COLLECTION", SOURCE_COLLECTION), ("STORAGE_DIR", source.target.storage_dir),
                        ("DB_SCHEMA", None)):
        monkeypatch.setattr(config, name, value)
    assert backup.main(["--to", str(tmp_path / "backups"), "--pg-container", "no-such-container-anywhere"]) == 1
    assert "FAILED" in capsys.readouterr().err
