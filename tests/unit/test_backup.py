"""The backup folder format: what makes it valid, and everything that must make it invalid. No services needed."""
import hashlib
import json

import pytest

from rag import backup
from rag.backup import BackupError, PgTools, RestoreRefused
from rag.target import Target


def make_backup(tmp_path, name="rag-backup-20261113-100000", **manifest_over):
    """A valid backup folder written by hand in the documented format."""
    folder = tmp_path / name
    (folder / "storage" / "documents" / "d1" / "versions" / "v1").mkdir(parents=True)
    dump, snapshot = folder / "postgres.dump", folder / "qdrant.snapshot"
    dump.write_bytes(b"PGDMP fake dump")
    snapshot.write_bytes(b"fake qdrant snapshot")
    stored = folder / "storage" / "documents" / "d1" / "versions" / "v1" / "original.txt"
    stored.write_bytes(b"the original file")
    manifest = {
        "format_version": backup.FORMAT_VERSION, "status": "complete", "consistent": True,
        "artifacts": [{"name": "postgres.dump", "kind": "postgres", "size": dump.stat().st_size,
                       "sha256": hashlib.sha256(dump.read_bytes()).hexdigest()},
                      {"name": "qdrant.snapshot", "kind": "qdrant", "size": snapshot.stat().st_size,
                       "sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest()}],
        "storage": {"file_count": 1, "total_bytes": stored.stat().st_size, "files": [
            {"path": "documents/d1/versions/v1/original.txt", "size": stored.stat().st_size,
             "sha256": hashlib.sha256(stored.read_bytes()).hexdigest()}]},
        "postgres": {"migrations": ["0001_documents"]}, "qdrant": {"points_count": 3, "version": "1.20.0"},
        **manifest_over}
    backup.write_manifest(folder, manifest)
    return folder


def test_a_complete_untouched_backup_is_valid(tmp_path):
    assert backup.verify(make_backup(tmp_path)) == []


@pytest.mark.parametrize("damage, expected", [
    (lambda f: (f / "postgres.dump").write_bytes(b"PGDMP altered!!"), "postgres.dump does not match"),
    (lambda f: (f / "qdrant.snapshot").write_bytes(b"short"), "qdrant.snapshot has a different size"),
    (lambda f: (f / "qdrant.snapshot").unlink(), "qdrant.snapshot is missing"),
    (lambda f: (f / "storage/documents/d1/versions/v1/original.txt").write_bytes(b"changed file."),
     "does not match its recorded size or SHA-256"),
    (lambda f: (f / "storage/documents/d1/versions/v1/original.txt").unlink(), "original.txt is missing"),
    (lambda f: (f / "storage" / "extra.txt").write_bytes(b"x"), "extra.txt is not in the manifest"),
    (lambda f: (f / backup.MANIFEST).write_text("{not json", encoding="utf-8"), "manifest.json cannot be read"),
    (lambda f: (f / backup.MANIFEST).unlink(), "manifest.json cannot be read"),
])
def test_every_kind_of_damage_makes_the_backup_invalid(tmp_path, damage, expected):
    folder = make_backup(tmp_path)
    damage(folder)
    problems = backup.verify(folder)
    assert problems and any(expected in p for p in problems)


@pytest.mark.parametrize("over, expected", [({"status": "in_progress"}, "'in_progress'"),
                                            ({"status": "failed"}, "'failed'"),
                                            ({"status": "inconsistent"}, "'inconsistent'"),
                                            ({"format_version": 99}, "unknown format version"),
                                            ({"consistent": False}, "changed while the backup was taken")])
def test_a_backup_that_is_not_complete_and_consistent_is_invalid(tmp_path, over, expected):
    problems = backup.verify(make_backup(tmp_path, **over))
    assert any(expected in p for p in problems)


def test_an_inconsistent_backup_can_be_accepted_only_on_purpose(tmp_path):
    folder = make_backup(tmp_path, consistent=False)
    assert backup.verify(folder) != [] and backup.verify(folder, allow_inconsistent=True) == []


@pytest.mark.parametrize("suffix", [".partial", ".failed", ".inconsistent"])
def test_a_folder_marked_unfinished_is_never_a_backup(tmp_path, suffix):
    folder = make_backup(tmp_path, name=f"rag-backup-20261113-100000{suffix}")
    assert any("not a finished backup" in p for p in backup.verify(folder))
    assert backup.verify(folder, building=True) == []  # only a backup checking itself just before its final name


# ---------- restore refuses before it touches anything ----------

class UntouchableTarget(Target):
    """A target that fails the test if anything even tries to connect to it."""

    def connect(self, **kwargs):
        raise AssertionError("restore must refuse before it connects to the target")

    def qdrant(self, timeout=60):
        raise AssertionError("restore must refuse before it connects to Qdrant")


def test_a_damaged_backup_is_refused_before_the_target_is_contacted(tmp_path):
    folder = make_backup(tmp_path)
    (folder / "postgres.dump").write_bytes(b"PGDMP altered!!")
    target = UntouchableTarget("postgresql://x/y", "http://q", "c", tmp_path / "new-storage")
    with pytest.raises(RestoreRefused, match="not valid"):
        backup.restore(folder, target, PgTools("nowhere"))
    assert not (tmp_path / "new-storage").exists()


def test_a_failed_or_partial_folder_cannot_be_restored(tmp_path):
    folder = make_backup(tmp_path, name="rag-backup-20261113-100000.failed", status="failed")
    target = UntouchableTarget("postgresql://x/y", "http://q", "c", tmp_path / "new-storage")
    with pytest.raises(RestoreRefused):
        backup.restore(folder, target, PgTools("nowhere"))


def test_the_restore_command_needs_every_target_option(capsys):
    with pytest.raises(SystemExit):  # argparse: the three targets are required, nothing defaults to the live system
        backup.restore_main(["some-folder"])
    assert "--database-url" in capsys.readouterr().err


def test_the_restore_command_reports_a_refusal_with_exit_code_1(tmp_path, capsys):
    folder = make_backup(tmp_path, status="failed")
    code = backup.restore_main([str(folder), "--database-url", "postgresql://x/y", "--qdrant-url", "http://q",
                                "--storage", str(tmp_path / "s")])
    assert code == 1 and "REFUSED" in capsys.readouterr().err


def test_the_verify_command_exit_codes(tmp_path, capsys):
    good = make_backup(tmp_path)
    assert backup.main(["--verify", str(good)]) == 0 and "VALID" in capsys.readouterr().out
    (good / "postgres.dump").write_bytes(b"PGDMP altered!!")
    assert backup.main(["--verify", str(good), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["valid"] is False


# ---------- helpers ----------

def test_copy_with_hash_copies_exactly_and_reports_size_and_hash(tmp_path):
    source = tmp_path / "in.bin"
    source.write_bytes(b"x" * 3_000_000)
    size, digest = backup.copy_with_hash(source, tmp_path / "deep" / "out.bin")
    assert size == 3_000_000 and digest == hashlib.sha256(b"x" * 3_000_000).hexdigest()
    assert (tmp_path / "deep" / "out.bin").read_bytes() == source.read_bytes()


def test_storage_listing_skips_uploads_in_progress_and_is_stable(tmp_path):
    for relative in ("b/2.txt", "a/1.txt", ".uploading/half", "a/0.txt"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    assert [relative for relative, _ in backup.storage_files(tmp_path)] == ["a/0.txt", "a/1.txt", "b/2.txt"]


def test_two_backups_in_the_same_second_get_different_names(tmp_path):
    (tmp_path / "rag-backup-x").mkdir()
    (tmp_path / "rag-backup-x-2.failed").mkdir()
    assert backup._unique(tmp_path, "rag-backup-x") == "rag-backup-x-3"


# ---------- how pg_dump / pg_restore are reached ----------

def test_a_named_container_is_used_as_given(tmp_path):
    target = Target("postgresql://rag:pw@h:5433/rag", "http://q", "c", tmp_path)
    command, env = PgTools("my-pg").command("pg_dump", ["-U", "rag"], target)
    assert command[:5] == ["docker", "exec", "-i", "my-pg", "pg_dump"] and env is None


def test_a_local_binary_gets_the_password_only_through_its_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(backup.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    target = Target("postgresql://rag:hunter2@db.example:5433/rag", "http://q", "c", tmp_path)
    command, env = PgTools().command("pg_dump", ["-U", "rag"], target)
    assert "hunter2" not in " ".join(command) and env["PGPASSWORD"] == "hunter2"
    assert command[:5] == ["/usr/bin/pg_dump", "-h", "db.example", "-p", "5433"]


def test_without_a_local_binary_the_compose_service_is_used(tmp_path, monkeypatch):
    monkeypatch.setattr(backup.shutil, "which", lambda tool: None)
    target = Target("postgresql://rag:pw@h:5433/rag", "http://q", "c", tmp_path)
    command, env = PgTools().command("pg_restore", [], target)
    assert command[:2] == ["docker", "compose"] and "exec" in command and command[-1] == "pg_restore" and env is None
    assert "pw" not in " ".join(command)


def test_a_failing_pg_dump_is_an_error_with_a_redacted_reason(tmp_path, monkeypatch):
    class Failed:
        returncode = 1
        stderr = b"pg_dump: error: connection to postgresql://rag:hunter2@db failed"

    monkeypatch.setattr(backup.subprocess, "run", lambda *a, **k: Failed())
    target = Target("postgresql://rag:pw@h:5433/rag", "http://q", "c", tmp_path)
    with pytest.raises(BackupError) as error:
        PgTools("c").dump(target, tmp_path / "out.dump")
    assert "hunter2" not in str(error.value) and "exit 1" in str(error.value)
