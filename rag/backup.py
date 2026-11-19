"""Backup and restore of everything the system keeps: PostgreSQL (the source of truth), the original files and the
Qdrant collection. No new dependency: pg_dump / pg_restore run inside the PostgreSQL container (or from the host if
they are installed) and Qdrant is reached through its own snapshot API.

    python -m rag backup  [--to DIR] [--wait-idle SECONDS] [--pg-container NAME] [--verify DIR]
    python -m rag restore DIR --database-url URL --qdrant-url URL --storage PATH [--collection NAME]

BACKUP FOLDER  <backups>/rag-backup-YYYYmmdd-HHMMSS/
    manifest.json    what the backup holds, written first as "in_progress" and last as "complete"
    postgres.dump    pg_dump -Fc of the whole database (schema, data, migration history)
    storage/         every original file, with its SHA-256 in the manifest
    qdrant.snapshot  the collection snapshot (vectors, payloads, payload indexes)
The folder is built as <name>.partial and renamed only when everything is written and checked. A backup that failed is
renamed <name>.failed, one that changed while it ran <name>.inconsistent; neither is ever called a backup, and
`verify` and `restore` refuse them.

CONSISTENCY  PostgreSQL's dump is one transaction (a single point in time). Original files never change once saved,
and a Qdrant snapshot is atomic. What can still go wrong is activity during the minutes the three copies take: an
upload, an activation, a rollback or a delete. Writers are not stopped (nothing is killed). Instead a fingerprint is
taken before and after (counts, the newest change time and the activation generations in PostgreSQL, the number of
points in Qdrant, the number and size of stored files); if it moved, the backup is marked inconsistent and the command
exits with 2. `--wait-idle` first waits until nothing is queued or being indexed. Run it when nobody uploads.

RESTORE  only into an EMPTY, separate environment: it refuses a database that has tables, a Qdrant collection that
exists, or a storage folder that is not empty, so the running system can never be overwritten by mistake. It
verifies the backup's checksums first, restores the three parts, checks the migration history and runs
`reconcile` on the result.

The backup holds every document and the whole database. Keep it as private as the data itself; it contains no password
and no .env file.
"""
import argparse
import hashlib
import http.client
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import urlopen

from rag import config, reconcile
from rag.health import redact
from rag.target import Target

log = logging.getLogger(__name__)
FORMAT_VERSION = 1
MANIFEST = "manifest.json"
CHUNK = 1024 * 1024


class BackupError(RuntimeError):
    """A backup or restore step failed."""


class BackupInconsistent(BackupError):
    """The data changed while the backup was being taken."""


class RestoreRefused(BackupError):
    """Restore was not attempted: the backup is not valid or the target is not an empty, separate environment."""


def _now():
    return datetime.now(timezone.utc)


# ---------- files ----------

def sha256_file(path):
    with open(path, "rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def copy_with_hash(source, destination):
    """Copy a file and return (size, sha256), reading the source once."""
    digest, size = hashlib.sha256(), 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with open(source, "rb") as src, open(destination, "wb") as dst:
        while chunk := src.read(CHUNK):
            digest.update(chunk)
            dst.write(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def storage_files(root):
    """(relative posix path, absolute path) of every stored file, in a stable order; uploads in progress are skipped."""
    root = Path(root)
    found = []
    for folder, dirs, files in os.walk(root):
        if Path(folder) == root and ".uploading" in dirs:
            dirs.remove(".uploading")
        dirs.sort()
        for name in sorted(files):
            full = Path(folder) / name
            found.append((full.relative_to(root).as_posix(), full))
    return found


# ---------- PostgreSQL tools ----------

class PgTools:
    """Runs pg_dump / pg_restore: in a named container, else a local binary, else in the compose "postgres" service.

    Inside a container the tools connect over the container's own socket as the database user (no password travels
    anywhere). A local binary gets the password only through the PGPASSWORD variable of its own process.
    """

    def __init__(self, container=None, service="postgres"):
        self.container, self.service = container, service

    def command(self, tool, args, target):
        db = target.database_parts()
        if self.container:
            return ["docker", "exec", "-i", self.container, tool, *args], None
        local = shutil.which(tool)
        if local:
            host = ["-h", db["host"], "-p", str(db["port"])] if db["host"] else []
            password = urlsplit(target.database_url).password
            return [local, *host, *args], ({**os.environ, "PGPASSWORD": password} if password else None)
        compose = Path(config.ROOT) / "docker-compose.yml"
        return ["docker", "compose", "-f", str(compose), "exec", "-T", self.service, tool, *args], None

    def _credentials(self, target):
        db = target.database_parts()
        return ["-U", db["user"], "-d", db["database"]]

    def dump(self, target, output):
        args = [*self._credentials(target), "-Fc", "--no-owner", "--no-acl"]
        if target.db_schema:
            args += ["-n", target.db_schema]
        command, env = self.command("pg_dump", args, target)
        with open(output, "wb") as out:
            process = subprocess.run(command, stdout=out, stderr=subprocess.PIPE, env=env)
        if process.returncode != 0:
            raise BackupError(f"pg_dump failed (exit {process.returncode}): "
                              f"{redact(process.stderr.decode('utf-8', 'replace'))}")

    def restore(self, target, dump):
        args = [*self._credentials(target), "--no-owner", "--no-acl", "--exit-on-error", "--single-transaction"]
        command, env = self.command("pg_restore", args, target)
        with open(dump, "rb") as source:
            process = subprocess.run(command, stdin=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        if process.returncode != 0:
            raise BackupError(f"pg_restore failed (exit {process.returncode}): "
                              f"{redact(process.stderr.decode('utf-8', 'replace'))}")


# ---------- the consistency fingerprint ----------

def fingerprint(target, client):
    """Numbers that change whenever the data does: if they differ before and after a backup, it is not one snapshot."""
    with target.connect() as conn:
        conn.read_only = True
        row = conn.execute(
            "SELECT (SELECT count(*) FROM documents) AS documents, "
            "(SELECT count(*) FROM document_versions) AS versions, "
            "(SELECT coalesce(extract(epoch FROM max(updated_at))::text, '0') FROM document_versions) "
            "AS versions_changed, "
            "(SELECT coalesce(extract(epoch FROM max(updated_at))::text, '0') FROM documents) AS documents_changed, "
            "(SELECT coalesce(sum(activation_generation), 0) FROM documents) AS generations").fetchone()
    files = storage_files(target.storage_dir)
    return {**{k: (str(v) if k.endswith("_changed") else int(v)) for k, v in row.items()},
            "qdrant_points": client.count(target.collection, exact=True).count,
            "storage_files": len(files), "storage_bytes": sum(path.stat().st_size for _, path in files)}


def idle(target):
    """True when nothing is queued or being indexed."""
    with target.connect() as conn:
        conn.read_only = True
        return conn.execute("SELECT count(*) AS n FROM document_versions WHERE status IN "
                            "('QUEUED', 'PROCESSING', 'EMBEDDING', 'INDEXING', 'VERIFYING')").fetchone()["n"] == 0


# ---------- Qdrant ----------

def _qdrant_version(url):
    try:
        with urlopen(url.rstrip("/") + "/", timeout=10) as response:
            return json.load(response).get("version")
    except Exception:  # noqa: BLE001 - only informational
        return None


def _download_snapshot(url, collection, name, destination):
    address = f"{url.rstrip('/')}/collections/{quote(collection)}/snapshots/{quote(name)}"
    digest, size = hashlib.sha256(), 0
    with urlopen(address, timeout=3600) as response, open(destination, "wb") as out:
        while chunk := response.read(CHUNK):
            digest.update(chunk)
            out.write(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _upload_snapshot(url, collection, path):
    """POST the snapshot file to /collections/<name>/snapshots/upload (streamed multipart; waits until loaded)."""
    parts = urlsplit(url)
    connection = (http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection)(
        parts.hostname, parts.port or (443 if parts.scheme == "https" else 6333), timeout=3600)
    boundary = uuid.uuid4().hex
    head = (f'--{boundary}\r\nContent-Disposition: form-data; name="snapshot"; filename="{path.name}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n").encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    connection.putrequest("POST", f"/collections/{quote(collection)}/snapshots/upload?priority=snapshot&wait=true")
    connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
    connection.putheader("Content-Length", str(len(head) + path.stat().st_size + len(tail)))
    connection.endheaders()
    connection.send(head)
    with open(path, "rb") as handle:
        while chunk := handle.read(CHUNK):
            connection.send(chunk)
    connection.send(tail)
    response = connection.getresponse()
    body = response.read().decode("utf-8", "replace")
    connection.close()
    if response.status >= 300:
        raise BackupError(f"Qdrant refused the snapshot (HTTP {response.status}): {redact(body)}")


def _collection_facts(client, collection):
    info = client.get_collection(collection)
    vectors = info.config.params.vectors
    return {"points_count": client.count(collection, exact=True).count,
            "vector_size": getattr(vectors, "size", None),
            "distance": str(getattr(getattr(vectors, "distance", None), "value", None))}


# ---------- manifest ----------

def write_manifest(folder, manifest):
    temp = Path(folder) / f".{MANIFEST}.tmp"
    temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, Path(folder) / MANIFEST)


def read_manifest(folder):
    return json.loads((Path(folder) / MANIFEST).read_text(encoding="utf-8"))


def verify(folder, allow_inconsistent=False, building=False):
    """Every reason this folder is not a valid, complete, intact backup (an empty list means it is).
    `building` is only for a backup checking itself just before it takes its final name."""
    folder = Path(folder)
    problems = []
    if folder.suffix in (".partial", ".failed", ".inconsistent") and not building:
        problems.append(f"the folder is marked {folder.suffix[1:]}: it is not a finished backup")
    try:
        manifest = read_manifest(folder)
    except (OSError, ValueError) as error:
        return problems + [f"manifest.json cannot be read ({type(error).__name__})"]
    if manifest.get("format_version") != FORMAT_VERSION:
        problems.append(f"unknown format version {manifest.get('format_version')!r}")
    if manifest.get("status") != "complete":
        problems.append(f"the backup status is {manifest.get('status')!r}, not 'complete'")
    if manifest.get("consistent") is False and not allow_inconsistent:
        problems.append("the data changed while the backup was taken (consistent = false)")
    for artifact in manifest.get("artifacts", []):
        path = folder / artifact["name"]
        if not path.is_file():
            problems.append(f"{artifact['name']} is missing")
        elif path.stat().st_size != artifact["size"]:
            problems.append(f"{artifact['name']} has a different size than recorded")
        elif sha256_file(path) != artifact["sha256"]:
            problems.append(f"{artifact['name']} does not match its SHA-256")
    expected = {f["path"]: f for f in manifest.get("storage", {}).get("files", [])}
    present = {relative: path for relative, path in storage_files(folder / "storage")}
    for relative, entry in expected.items():
        path = present.get(relative)
        if path is None:
            problems.append(f"storage/{relative} is missing")
        elif path.stat().st_size != entry["size"] or sha256_file(path) != entry["sha256"]:
            problems.append(f"storage/{relative} does not match its recorded size or SHA-256")
    for relative in sorted(set(present) - set(expected)):
        problems.append(f"storage/{relative} is not in the manifest")
    return problems


# ---------- backup ----------

def _unique(root, name):
    candidate, n = name, 1
    while any((root / f"{candidate}{suffix}").exists() for suffix in ("", ".partial", ".failed", ".inconsistent")):
        n += 1
        candidate = f"{name}-{n}"
    return candidate


def _finish(partial, suffix, manifest):
    write_manifest(partial, manifest)
    destination = partial.with_name(partial.name.removesuffix(".partial") + suffix)
    os.replace(partial, destination)
    return destination


def backup(target=None, destination=None, pg=None, *, wait_idle=0, allow_inconsistent=False,
           keep_server_snapshot=False, on_phase=None):
    """Take a backup and return the finished folder. Raises BackupInconsistent / BackupError (and leaves a .failed or
    .inconsistent folder behind) instead of ever returning something that is not a complete backup."""
    target, pg = target or Target.from_config(), pg or PgTools()
    root = Path(destination or config.BACKUP_DIR)
    on_phase = on_phase or (lambda phase: None)
    client = target.qdrant(timeout=600)
    if not Path(target.storage_dir).is_dir():
        raise BackupError("the storage folder does not exist")
    if not client.collection_exists(target.collection):
        raise BackupError(f"the Qdrant collection {target.collection!r} does not exist")
    deadline = time.monotonic() + wait_idle
    while wait_idle and not idle(target):
        if time.monotonic() > deadline:
            raise BackupError("work is still queued or being indexed; try again when it is idle")
        time.sleep(5)

    root.mkdir(parents=True, exist_ok=True)
    name = _unique(root, f"rag-backup-{_now():%Y%m%d-%H%M%S}")
    partial = root / f"{name}.partial"
    partial.mkdir()
    started = _now()
    manifest = {"format_version": FORMAT_VERSION, "tool": "python -m rag backup", "status": "in_progress",
                "started_at": started.isoformat(), "source": target.describe(), "artifacts": []}
    write_manifest(partial, manifest)
    log.info("backup started: %s", partial.name)
    try:
        manifest["fingerprint_before"] = fingerprint(target, client)
        on_phase("fingerprint_before")

        dump = partial / "postgres.dump"
        t0 = time.monotonic()
        dump_started = _now()
        pg.dump(target, dump)
        with target.connect() as conn:
            conn.read_only = True
            version = conn.execute("SHOW server_version").fetchone()["server_version"]
            migrations = [r["version"] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
        manifest["postgres"] = {"server_version": version, "database": target.database_parts()["database"],
                                "dump_started_at": dump_started.isoformat(), "dump_finished_at": _now().isoformat(),
                                "migrations": migrations, "seconds": round(time.monotonic() - t0, 2)}
        manifest["artifacts"].append({"name": "postgres.dump", "kind": "postgres", "size": dump.stat().st_size,
                                      "sha256": sha256_file(dump)})
        on_phase("postgres")

        t0 = time.monotonic()
        files = []
        for relative, source in storage_files(target.storage_dir):
            size, digest = copy_with_hash(source, partial / "storage" / relative)
            files.append({"path": relative, "size": size, "sha256": digest})
        (partial / "storage").mkdir(exist_ok=True)
        manifest["storage"] = {"file_count": len(files), "total_bytes": sum(f["size"] for f in files),
                               "seconds": round(time.monotonic() - t0, 2), "files": files}
        on_phase("storage")

        t0 = time.monotonic()
        snapshot = client.create_snapshot(target.collection, wait=True)
        snapshot_path = partial / "qdrant.snapshot"
        size, digest = _download_snapshot(target.qdrant_url, target.collection, snapshot.name, snapshot_path)
        if snapshot.size and size != snapshot.size:
            raise BackupError("the downloaded Qdrant snapshot has a different size than the server reported")
        manifest["qdrant"] = {"version": _qdrant_version(target.qdrant_url), "collection": target.collection,
                              "snapshot_name": snapshot.name, "snapshot_created_at": str(snapshot.creation_time),
                              **_collection_facts(client, target.collection),
                              "seconds": round(time.monotonic() - t0, 2)}
        manifest["artifacts"].append({"name": "qdrant.snapshot", "kind": "qdrant", "size": size, "sha256": digest})
        if not keep_server_snapshot:
            client.delete_snapshot(target.collection, snapshot.name, wait=True)  # only the copy this backup created
        on_phase("qdrant")

        manifest["fingerprint_after"] = fingerprint(target, client)
        manifest["consistent"] = manifest["fingerprint_before"] == manifest["fingerprint_after"]
        manifest["finished_at"] = _now().isoformat()
        manifest["seconds"] = round((_now() - started).total_seconds(), 2)
        if not manifest["consistent"] and not allow_inconsistent:
            manifest["status"] = "inconsistent"
            moved = _finish(partial, ".inconsistent", manifest)
            raise BackupInconsistent(f"the data changed while the backup was taken; kept as {moved.name}, "
                                     "it is not a valid backup")
        manifest["status"] = "complete"
        write_manifest(partial, manifest)
        problems = verify(partial, allow_inconsistent=True, building=True)  # it must pass its own check first
        if problems:
            raise BackupError("the finished backup failed its own check: " + "; ".join(problems[:3]))
        final = partial.with_name(name)
        os.replace(partial, final)
        log.info("backup complete: %s (%d storage files, %d Qdrant points)", final.name,
                 manifest["storage"]["file_count"], manifest["qdrant"]["points_count"])
        return final
    except BackupInconsistent:
        raise
    except Exception as error:
        manifest.update(status="failed", error=redact(f"{type(error).__name__}: {error}"),
                        finished_at=_now().isoformat())
        failed = _finish(partial, ".failed", manifest)
        raise BackupError(f"backup failed: {redact(error)} (kept as {failed.name}, not a valid backup)") from error


# ---------- restore ----------

def _check_empty_target(target, client, problems):
    try:
        with target.connect() as conn:
            conn.read_only = True
            tables = conn.execute("SELECT count(*) AS n FROM information_schema.tables WHERE table_schema = %s",
                                  (target.db_schema or "public",)).fetchone()["n"]
        if tables:
            problems.append(f"the target database is not empty ({tables} table(s)): restore only into a new, empty one")
    except Exception as error:  # noqa: BLE001
        problems.append(f"cannot connect to the target database ({type(error).__name__})")
    try:
        if client.collection_exists(target.collection):
            problems.append(f"the Qdrant collection {target.collection!r} already exists on the target")
    except Exception as error:  # noqa: BLE001
        problems.append(f"cannot reach the target Qdrant ({type(error).__name__})")
    storage = Path(target.storage_dir)
    if storage.exists() and (not storage.is_dir() or any(storage.iterdir())):
        problems.append(f"the target storage folder {storage} is not empty")


def restore(folder, target, pg=None, *, run_reconcile=True, allow_inconsistent=False, on_phase=None):
    """Restore a verified backup into an empty, separate environment and return what happened (with per-phase seconds).
    Raises RestoreRefused before touching anything if the backup is invalid or the target is not empty."""
    pg, folder = pg or PgTools(), Path(folder)
    on_phase = on_phase or (lambda phase: None)
    seconds, warnings = {}, []
    started = time.monotonic()

    t0 = time.monotonic()
    problems = verify(folder, allow_inconsistent=allow_inconsistent)
    seconds["verify"] = round(time.monotonic() - t0, 2)
    if problems:
        raise RestoreRefused("the backup is not valid: " + "; ".join(problems[:5]))
    manifest = read_manifest(folder)
    client = target.qdrant(timeout=3600)
    problems = []
    _check_empty_target(target, client, problems)
    if problems:
        raise RestoreRefused("; ".join(problems))
    source_version, target_version = manifest["qdrant"].get("version"), _qdrant_version(target.qdrant_url)
    if source_version and target_version and source_version.rsplit(".", 1)[0] != target_version.rsplit(".", 1)[0]:
        warnings.append(f"Qdrant version differs (backup {source_version}, target {target_version}); snapshots are "
                        "only guaranteed between the same minor versions")
    on_phase("preflight")

    t0 = time.monotonic()
    pg.restore(target, folder / "postgres.dump")
    seconds["postgres"] = round(time.monotonic() - t0, 2)
    with target.connect() as conn:
        conn.read_only = True
        migrations = [r["version"] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
    if migrations != manifest["postgres"]["migrations"]:
        raise BackupError("the restored migration history differs from the backup's")
    on_phase("postgres")

    t0 = time.monotonic()
    storage_root = Path(target.storage_dir)
    storage_root.mkdir(parents=True, exist_ok=True)
    for entry in manifest["storage"]["files"]:
        size, digest = copy_with_hash(folder / "storage" / entry["path"], storage_root / entry["path"])
        if size != entry["size"] or digest != entry["sha256"]:
            raise BackupError(f"storage/{entry['path']} changed while it was restored")
    seconds["storage"] = round(time.monotonic() - t0, 2)
    on_phase("storage")

    t0 = time.monotonic()
    _upload_snapshot(target.qdrant_url, target.collection, folder / "qdrant.snapshot")
    restored = client.count(target.collection, exact=True).count
    if restored != manifest["qdrant"]["points_count"]:
        raise BackupError(f"Qdrant holds {restored} points after the restore, the backup had "
                          f"{manifest['qdrant']['points_count']}")
    seconds["qdrant"] = round(time.monotonic() - t0, 2)
    on_phase("qdrant")

    result = {"backup": folder.name, "seconds": seconds, "warnings": warnings, "reconcile": None, "ok": True}
    if run_reconcile:
        t0 = time.monotonic()
        report = reconcile.run(target)
        seconds["reconcile"] = round(time.monotonic() - t0, 2)
        result["reconcile"] = report.to_dict()["summary"]
        result["ok"] = report.exit_code == 0
    seconds["total"] = round(time.monotonic() - started, 2)
    return result


# ---------- command line ----------

def main(argv):
    parser = argparse.ArgumentParser(prog="python -m rag backup", description="PostgreSQL + storage + Qdrant snapshot "
                                     "into one verified backup folder.")
    parser.add_argument("--to", type=Path, default=None, help=f"where to put it (default: {config.BACKUP_DIR})")
    parser.add_argument("--pg-container", help="Docker container that runs PostgreSQL (default: the compose service)")
    parser.add_argument("--wait-idle", type=int, default=0, metavar="SECONDS",
                        help="wait up to this long for the queue to empty before starting")
    parser.add_argument("--allow-inconsistent", action="store_true",
                        help="keep the backup even if data changed during it")
    parser.add_argument("--keep-server-snapshot", action="store_true",
                        help="leave the snapshot on the Qdrant server too")
    parser.add_argument("--verify", type=Path, metavar="DIR", help="only check an existing backup folder")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args(argv)
    if args.verify:
        problems = verify(args.verify, allow_inconsistent=args.allow_inconsistent)
        print(json.dumps({"backup": str(args.verify), "valid": not problems, "problems": problems})
              if args.json else ("VALID" if not problems else "NOT VALID:\n  " + "\n  ".join(problems)))
        return 0 if not problems else 1
    try:
        folder = backup(destination=args.to, pg=PgTools(args.pg_container), wait_idle=args.wait_idle,
                        allow_inconsistent=args.allow_inconsistent, keep_server_snapshot=args.keep_server_snapshot)
    except BackupInconsistent as error:
        print(f"INCONSISTENT: {error}", file=sys.stderr)
        return 2
    except BackupError as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 1
    manifest = read_manifest(folder)
    summary = {"backup": str(folder), "status": manifest["status"], "seconds": manifest["seconds"],
               "storage_files": manifest["storage"]["file_count"], "storage_bytes": manifest["storage"]["total_bytes"],
               "qdrant_points": manifest["qdrant"]["points_count"], "consistent": manifest["consistent"]}
    print(json.dumps(summary) if args.json else "backup complete: " + ", ".join(f"{k}={v}" for k, v in summary.items()))
    return 0


def restore_main(argv):
    parser = argparse.ArgumentParser(
        prog="python -m rag restore",
        description="Restore a backup into an EMPTY, separate environment. All three target options are required on "
                    "purpose: nothing defaults to the running system.")
    parser.add_argument("backup", type=Path)
    parser.add_argument("--database-url", required=True, help="the new, empty PostgreSQL database")
    parser.add_argument("--qdrant-url", required=True, help="the new Qdrant (the collection must not exist)")
    parser.add_argument("--storage", required=True, type=Path, help="the new, empty storage folder")
    parser.add_argument("--collection", default=config.COLLECTION)
    parser.add_argument("--db-schema", default=None)
    parser.add_argument("--pg-container", help="Docker container that runs the TARGET PostgreSQL")
    parser.add_argument("--no-reconcile", action="store_true", help="skip the check after the restore")
    parser.add_argument("--allow-inconsistent", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    target = Target(args.database_url, args.qdrant_url, args.collection, args.storage.resolve(), args.db_schema)
    try:
        result = restore(args.backup, target, PgTools(args.pg_container), run_reconcile=not args.no_reconcile,
                         allow_inconsistent=args.allow_inconsistent)
    except BackupError as error:
        print(f"RESTORE REFUSED OR FAILED: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2) if args.json else
          f"restored {result['backup']} in {result['seconds']['total']} s  {result['seconds']}\n"
          f"reconcile: {result['reconcile']}")
    return 0 if result["ok"] else 2
