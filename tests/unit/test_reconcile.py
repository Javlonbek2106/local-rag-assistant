"""Reconciliation: what it finds, and the proof that it only looks."""
import copy
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone

import pytest

from rag import reconcile
from rag.reconcile import Options, ReadOnlyQdrant, ReadOnlyViolation, Report
from rag.target import Target
from tests.conftest import FakeQdrant

COLLECTION = "documents_v2"


class World:
    """One document with one READY version: a file on disk, chunks in Qdrant, rows as PostgreSQL would return them."""

    def __init__(self, tmp_path, chunks=3):
        self.storage = tmp_path / "storage"
        self.doc_id, self.ver_id = str(uuid.uuid4()), str(uuid.uuid4())
        self.relative = f"documents/{self.doc_id}/versions/{self.ver_id}/original.txt"
        self.path = self.storage / self.relative
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"original bytes")
        self.hash = hashlib.sha256(b"original bytes").hexdigest()
        self.qdrant = FakeQdrant()
        self.state = {
            "documents": [{"id": self.doc_id, "name": "a.txt", "active_version_id": self.ver_id, "state": "done"}],
            "versions": [{"id": self.ver_id, "document_id": self.doc_id, "version_number": 1, "file_name": "a.txt",
                          "file_hash": self.hash, "file_path": self.relative, "status": "READY",
                          "chunk_count": chunks}],
        }
        for i in range(chunks):
            self.qdrant.points[f"p{i}"] = ([0.1], self.payload(i, chunks))

    def payload(self, index, total, **over):
        return {"document_id": self.doc_id, "version_id": self.ver_id, "file_hash": self.hash, "chunks_total": total,
                "chunk_index": index, "source": self.doc_id, **over}

    @property
    def version(self):
        return self.state["versions"][0]

    def add_version(self, status, number=2, chunks=2, file=True, **over):
        ver_id = str(uuid.uuid4())
        relative = f"documents/{self.doc_id}/versions/{ver_id}/original.txt"
        if file:
            (self.storage / relative).parent.mkdir(parents=True)
            (self.storage / relative).write_bytes(b"older bytes")
        version = {"id": ver_id, "document_id": self.doc_id, "version_number": number, "file_name": "a.txt",
                   "file_hash": hashlib.sha256(b"older bytes").hexdigest(), "file_path": relative, "status": status,
                   "chunk_count": chunks, **over}
        self.state["versions"].append(version)
        return version

    def run(self, **options):
        report = Report(Target("postgresql://x/y", "http://q", COLLECTION, self.storage), Options(**options))
        reconcile.check(self.state, ReadOnlyQdrant(self.qdrant), COLLECTION, self.storage, report.options, report)
        return report


def codes(report):
    return {issue["code"] for issue in report.issues}


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


# ---------- the clean case ----------

def test_a_consistent_system_is_clean(world):
    report = world.run()
    assert report.issues == [] and report.exit_code == 0
    checked = report.to_dict()["checked"]
    assert checked["documents"] == 1 and checked["versions"] == 1 and checked["qdrant_points"] == 3
    assert checked["files_hashed"] == 1 and checked["versions_checked_deeply"] == 1 and checked["storage_files"] == 1


# ---------- what must be found ----------

def test_a_vector_deleted_by_hand_is_found(world):
    del world.qdrant.points["p1"]  # exactly what a lost or damaged index looks like
    report = world.run()
    (issue,) = report.issues
    assert issue["code"] == "chunk_count_mismatch" and issue["severity"] == "error"
    assert (issue["subject"]["expected"], issue["subject"]["found"]) == (3, 2)
    assert issue["subject"]["document"] == "a.txt"
    assert "missing from the index" in issue["message"] and report.exit_code == 1


def test_an_emptied_index_is_found_for_every_ready_version(world):
    world.qdrant.points.clear()
    report = world.run()
    assert report.exit_code == 1 and report.issues[0]["subject"]["found"] == 0


def test_a_missing_collection_is_one_clear_error(world):
    world.qdrant.missing_collection = True
    world.qdrant.points.clear()
    report = world.run()
    assert "collection_missing" in codes(report) and report.exit_code == 1


def test_more_points_than_recorded_is_found(world):
    world.qdrant.points["extra"] = ([0.1], world.payload(2, 3))
    report = world.run()
    assert codes(report) >= {"chunk_count_mismatch"} and "more points" in report.issues[0]["message"]


def test_points_of_a_version_postgres_does_not_know_are_orphans(world):
    ghost = str(uuid.uuid4())
    for i in range(4):
        world.qdrant.points[f"ghost{i}"] = ([0.1], {"document_id": "gone", "version_id": ghost, "chunk_index": i})
    report = world.run()
    (issue,) = report.issues
    assert issue["code"] == "orphan_points" and issue["severity"] == "warning" and issue["subject"]["points"] == 4
    assert report.exit_code == 2  # waste, not damage


def test_points_without_any_version_id_are_reported(world):
    world.qdrant.points["old"] = ([0.1], {"source": world.doc_id, "text": "indexed before versioning"})
    assert "untagged_points" in codes(world.run())


def test_points_left_by_a_failed_version_are_reported(world):
    failed = world.add_version("FAILED", number=2, file=False)
    world.qdrant.points["partial"] = ([0.1], {"version_id": failed["id"], "document_id": world.doc_id})
    report = world.run()
    assert "failed_version_points" in codes(report) and report.exit_code == 2


def test_a_stored_file_that_no_version_refers_to_is_an_orphan(world):
    stray = world.storage / "documents" / "unknown-doc" / "versions" / "v" / "original.pdf"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"x" * 10)
    (world.storage / ".uploading").mkdir()
    (world.storage / ".uploading" / "half-written").write_bytes(b"x")  # an upload in progress is not an orphan
    report = world.run()
    (issue,) = report.issues
    assert issue["code"] == "orphan_file" and issue["subject"]["bytes"] == 10
    assert issue["subject"]["path"] == "documents/unknown-doc/versions/v/original.pdf"


def test_a_file_postgres_refers_to_but_the_disk_lacks_is_found(world):
    world.path.unlink()
    report = world.run()
    assert report.issues[0]["code"] == "missing_file" and report.exit_code == 1
    assert "hash_mismatch" not in codes(report)


def test_a_changed_file_is_a_hash_mismatch(world):
    world.path.write_bytes(b"someone edited this")
    report = world.run()
    assert [i["code"] for i in report.issues] == ["hash_mismatch"] and report.exit_code == 1


def test_no_hashing_option_skips_only_the_hash(world):
    world.path.write_bytes(b"someone edited this")
    assert world.run(hash_files=False).issues == []


def test_a_version_without_a_recorded_hash_is_a_warning(world):
    world.version["file_hash"] = None
    report = world.run(scan_payloads=False)
    assert [i["code"] for i in report.issues] == ["hash_unknown"] and report.exit_code == 2


def test_a_recorded_path_that_leaves_the_storage_folder_is_an_error(world):
    world.version["file_path"] = "../../etc/passwd"
    assert "file_path_outside_storage" in codes(world.run())


# ---------- the active pointer ----------

def test_the_active_version_must_exist_and_be_ready(world):
    world.state["documents"][0]["active_version_id"] = str(uuid.uuid4())
    assert "active_version_missing" in codes(world.run())
    world.state["documents"][0]["active_version_id"] = world.ver_id
    world.version["status"] = "SUPERSEDED"
    assert "active_version_not_ready" in codes(world.run())


def test_a_ready_version_that_is_not_the_active_one_is_found(world):
    world.add_version("READY", number=2, chunks=0, file=True)
    report = world.run()
    found = [i["subject"]["version"] for i in report.issues if i["code"] == "ready_version_not_active"]
    assert found == [2] and report.exit_code == 1


# ---------- the points' own identifiers ----------

@pytest.mark.parametrize("over, key", [({"document_id": "another-document"}, "document_id"),
                                       ({"file_hash": "different"}, "file_hash"),
                                       ({"chunks_total": 99}, "chunks_total")])
def test_points_with_wrong_identifiers_are_found(world, over, key):
    world.qdrant.points["p0"] = ([0.1], world.payload(0, 3, **over))
    report = world.run()
    (issue,) = report.issues
    assert issue["code"] == "payload_invalid" and key in issue["subject"]["problems"]


def test_duplicate_and_out_of_range_chunk_indexes_are_found(world):
    world.qdrant.points["p1"] = ([0.1], world.payload(0, 3))  # index 0 twice, index 1 never
    world.qdrant.points["p2"] = ([0.1], world.payload(7, 3))  # out of range
    (issue,) = world.run().issues
    assert issue["subject"]["problems"] == {"duplicate_chunk_index": 1, "chunk_index": 1}


def test_quick_mode_does_not_read_the_points_but_still_counts_them(world):
    world.qdrant.points["p0"] = ([0.1], world.payload(0, 3, file_hash="different"))
    assert world.run(scan_payloads=False).issues == []
    del world.qdrant.points["p1"]
    assert codes(world.run(scan_payloads=False)) == {"chunk_count_mismatch"}


# ---------- versions kept for rollback ----------

def test_superseded_versions_are_checked_only_when_asked(world):
    old = world.add_version("SUPERSEDED", number=2, chunks=2)  # its vectors are gone
    assert world.run().issues == []
    report = world.run(include_superseded=True)
    assert [i["code"] for i in report.issues] == ["chunk_count_mismatch"]
    assert report.issues[0]["subject"]["version"] == old["version_number"] and report.exit_code == 1


def test_a_missing_file_of_a_kept_version_is_an_error_even_by_default(world):
    world.add_version("SUPERSEDED", number=2, chunks=0, file=False)
    assert "missing_file" in codes(world.run())


def test_an_upload_still_being_saved_is_not_a_missing_file(world):
    world.add_version("UPLOADED", number=2, chunks=0, file=False)
    assert world.run().issues == []


# ---------- the report ----------

def test_only_the_first_examples_are_listed_but_every_problem_is_counted(world):
    for i in range(30):
        stray = world.storage / "documents" / f"orphan{i}" / "original.txt"
        stray.parent.mkdir(parents=True)
        stray.write_bytes(b"x")
    report = world.run(example_limit=5)
    data = report.to_dict()
    assert len(report.issues) == 5 and data["summary"]["by_code"]["orphan_file"]["count"] == 30
    assert "30 in all" in reconcile.render(report)


def test_the_report_json_has_no_secrets_and_is_complete(world):
    target = Target("postgresql://rag:SuperSecret@db.example:5433/rag", "http://q:6333", COLLECTION, world.storage)
    report = Report(target, Options())
    text = json.dumps(report.to_dict())
    assert "SuperSecret" not in text and "db.example" in text and "rag" in text


# ---------- READ ONLY: proved, not promised ----------

def test_the_qdrant_proxy_refuses_every_call_that_could_change_something(world):
    proxy = ReadOnlyQdrant(world.qdrant)
    for call in (lambda: proxy.delete(COLLECTION, points_selector=None),
                 lambda: proxy.upload_points(COLLECTION, points=[]),
                 lambda: proxy.set_payload(COLLECTION, payload={}, points=None),
                 lambda: proxy.create_snapshot(COLLECTION),
                 lambda: proxy.delete_collection(COLLECTION),
                 lambda: proxy.create_payload_index(COLLECTION, "x", None),
                 lambda: proxy.recreate_collection(COLLECTION)):
        with pytest.raises(ReadOnlyViolation):
            call()
    assert len(world.qdrant.points) == 3 and world.qdrant.deleted_filters == []
    assert proxy.count(COLLECTION, exact=True).count == 3  # reading still works


def snapshot_of(world):
    files = {str(p.relative_to(world.storage)): (p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes())
             for p in world.storage.rglob("*") if p.is_file()}
    return files, copy.deepcopy(world.qdrant.points), copy.deepcopy(world.state), sorted(os.listdir(world.storage))


def test_a_full_reconcile_with_findings_changes_nothing_anywhere(world):
    del world.qdrant.points["p1"]
    world.path.write_bytes(b"tampered")
    world.qdrant.points["ghost"] = ([0.1], {"version_id": "unknown"})
    (world.storage / "documents" / "orphan").mkdir(parents=True)
    (world.storage / "documents" / "orphan" / "f.txt").write_bytes(b"x")
    before = snapshot_of(world)
    report = world.run(include_superseded=True)
    assert report.exit_code == 1 and len(report.issues) >= 3  # it did find things ...
    assert snapshot_of(world) == before  # ... and touched none of them
    assert world.qdrant.deleted_filters == []


# ---------- the last result and the command ----------

def test_the_last_report_is_kept_and_read_back(world, tmp_path):
    report = world.run()
    report.finished_at = datetime.now(timezone.utc)
    reconcile.save_last(report, tmp_path / "reports")
    last = reconcile.load_last(tmp_path / "reports")
    assert last["ok"] is True and last["errors"] == 0 and last["age_seconds"] >= 0
    assert not list((tmp_path / "reports").glob(".*tmp"))  # replaced atomically, no temp file left behind


def test_a_missing_or_damaged_last_report_reads_as_none(tmp_path):
    assert reconcile.load_last(tmp_path / "none") is None
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / reconcile.REPORT_NAME).write_text("{not json", encoding="utf-8")
    assert reconcile.load_last(tmp_path / "reports") is None


@pytest.mark.parametrize("damage, exit_code", [(None, 0), ("orphan", 2), ("vector", 1)])
def test_the_command_exit_code_tells_clean_warnings_and_errors_apart(world, monkeypatch, capsys, tmp_path, damage,
                                                                     exit_code):
    if damage == "orphan":
        world.qdrant.points["ghost"] = ([0.1], {"version_id": "unknown"})
    elif damage == "vector":
        del world.qdrant.points["p0"]
    monkeypatch.setattr(reconcile, "run", lambda target=None, options=None: world.run())
    monkeypatch.setattr(reconcile.config, "REPORT_DIR", tmp_path / "reports")
    assert reconcile.main(["--json"]) == exit_code
    printed = json.loads(capsys.readouterr().out)
    assert printed["summary"]["ok"] is (exit_code == 0)
    assert (tmp_path / "reports" / reconcile.REPORT_NAME).is_file()  # kept for /api/status


def test_the_command_does_not_keep_a_report_when_asked_not_to(world, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(reconcile, "run", lambda target=None, options=None: world.run())
    monkeypatch.setattr(reconcile.config, "REPORT_DIR", tmp_path / "reports")
    assert reconcile.main(["--no-save"]) == 0
    assert not (tmp_path / "reports").exists()
    assert "CLEAN" in capsys.readouterr().out


def test_a_check_that_cannot_run_says_so_with_its_own_exit_code_and_no_secrets(monkeypatch, capsys):
    def boom(target=None, options=None):
        raise ConnectionError("could not connect to postgresql://rag:SuperSecret@db/rag")

    monkeypatch.setattr(reconcile, "run", boom)
    assert reconcile.main([]) == 3
    captured = capsys.readouterr()
    assert "SuperSecret" not in captured.out + captured.err and "ConnectionError" in captured.err
