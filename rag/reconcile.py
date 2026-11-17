"""Read-only reconciliation of the three stores: PostgreSQL (the source of truth), Qdrant (the derived index) and the
stored original files.

    python -m rag reconcile [--json] [--no-hash] [--quick] [--include-superseded] [--no-save]

It only reports. It never deletes, adds, updates, moves, requeues, activates or rolls back anything, and that is
enforced rather than promised:

- PostgreSQL is read in a READ ONLY, REPEATABLE READ transaction (a write raises an error from the server);
- Qdrant is reached through ReadOnlyQdrant, which answers count, scroll, facet and the two metadata calls and raises
  ReadOnlyViolation for every other method;
- files are only listed, `stat`ed and opened with "rb".
The one thing written is this report itself (REPORT_DIR/reconcile-last.json), which is not part of any of the three
stores. Fixing what it finds is a separate, deliberate step.

Exit code: 0 clean, 1 at least one error (the data is wrong or unavailable), 2 only warnings (waste, legacy leftovers).
"""
import argparse
import hashlib
import json
import logging
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from qdrant_client import models

from rag import config
from rag.target import Target

log = logging.getLogger(__name__)
ERROR, WARNING = "error", "warning"
FACET_LIMIT = 100_000  # distinct version ids one facet call can list; more is reported, never silently ignored
PAYLOAD_KEYS = ["document_id", "version_id", "file_hash", "chunks_total", "chunk_index"]
READ_ONLY_CALLS = frozenset({"count", "scroll", "facet", "get_collection", "collection_exists"})
REPORT_NAME = "reconcile-last.json"
# a version in these states has (or is about to have) its file on disk; UPLOADED is still being saved, FAILED may
# never have had one
FILE_EXPECTED = ("QUEUED", "PROCESSING", "EMBEDDING", "INDEXING", "VERIFYING", "READY", "SUPERSEDED")


class ReadOnlyViolation(RuntimeError):
    """Something tried to change Qdrant through the reconciliation client."""


class ReadOnlyQdrant:
    """A Qdrant client that can only look. Any other method name raises instead of reaching the server."""

    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        if name in READ_ONLY_CALLS:
            return getattr(self._client, name)
        raise ReadOnlyViolation(f"reconcile is read-only: the Qdrant call {name!r} is not allowed")


@dataclass
class Options:
    hash_files: bool = True  # read every file of a checked version and compare its SHA-256
    scan_payloads: bool = True  # read every point's identifiers of a checked version
    include_superseded: bool = False  # also check the versions kept for rollback, not only the READY ones
    batch: int = 500
    example_limit: int = 20  # issues listed per code; the totals always count all of them


class Report:
    def __init__(self, target, options):
        self.target, self.options = target, options
        self.started_at = datetime.now(timezone.utc)
        self.finished_at = None
        self.issues = []
        self.totals = Counter()
        self.severity = {}
        self.checked = Counter()

    def add(self, code, severity, message, **subject):
        self.totals[code] += 1
        self.severity[code] = severity
        if self.totals[code] <= self.options.example_limit:
            self.issues.append({"code": code, "severity": severity, "message": message, "subject": subject})

    @property
    def errors(self):
        return sum(n for code, n in self.totals.items() if self.severity[code] == ERROR)

    @property
    def warnings(self):
        return sum(n for code, n in self.totals.items() if self.severity[code] == WARNING)

    @property
    def exit_code(self):
        return 1 if self.errors else (2 if self.warnings else 0)

    def to_dict(self):
        finished = self.finished_at or datetime.now(timezone.utc)
        return {
            "started_at": self.started_at.isoformat(), "finished_at": finished.isoformat(),
            "duration_seconds": round((finished - self.started_at).total_seconds(), 2),
            "target": self.target.describe(),
            "options": {"hash_files": self.options.hash_files, "scan_payloads": self.options.scan_payloads,
                        "include_superseded": self.options.include_superseded},
            "checked": dict(self.checked),
            "summary": {"ok": self.exit_code == 0, "errors": self.errors, "warnings": self.warnings,
                        "by_code": {code: {"severity": self.severity[code], "count": n}
                                    for code, n in sorted(self.totals.items())}},
            "issues": self.issues,
        }


def load_state(target):
    """Every document and version row, read in one READ ONLY, REPEATABLE READ transaction."""
    with target.connect() as conn:
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        conn.read_only = True
        documents = conn.execute("SELECT id::text AS id, name, active_version_id::text AS active_version_id, state "
                                 "FROM documents ORDER BY name").fetchall()
        versions = conn.execute(
            "SELECT id::text AS id, document_id::text AS document_id, version_number, file_name, file_hash, file_path, "
            "status, chunk_count FROM document_versions ORDER BY document_id, version_number").fetchall()
    return {"documents": documents, "versions": versions}


def sha256_of(path):
    with open(path, "rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _resolve(storage_dir, relative):
    """The file's absolute path, or None when the recorded path would point outside the storage folder."""
    root = Path(storage_dir).resolve()
    path = (root / relative).resolve()
    return path if path.is_relative_to(root) else None


def _name(doc_names, version):
    return f"{doc_names.get(version['document_id'], '?')} v{version['version_number']}"


def check(state, qdrant, collection, storage_dir, options, report):
    """Compare PostgreSQL's state with Qdrant and the storage folder; every finding goes into `report`."""
    documents, versions = state["documents"], state["versions"]
    by_id = {v["id"]: v for v in versions}
    doc_names = {d["id"]: d["name"] for d in documents}
    report.checked["documents"], report.checked["versions"] = len(documents), len(versions)

    _check_pointers(documents, versions, by_id, doc_names, report)
    counts = _qdrant_counts(qdrant, collection, report)
    kinds = ("READY", "SUPERSEDED") if options.include_superseded else ("READY",)
    deep = [v for v in versions if v["status"] in kinds]
    report.checked["versions_checked_deeply"] = len(deep)
    deep_ids = {v["id"] for v in deep}

    for v in deep:
        _check_chunks(v, counts, doc_names, report)
        if options.scan_payloads and counts.get(v["id"]):
            _check_payloads(v, qdrant, collection, doc_names, options, report)
    for v in versions:
        if v["status"] in FILE_EXPECTED:
            _check_file(v, storage_dir, doc_names, options, report, hash_it=options.hash_files and v["id"] in deep_ids)
    _orphan_points(counts, by_id, doc_names, report)
    _orphan_files(versions, storage_dir, report)


def _check_pointers(documents, versions, by_id, doc_names, report):
    active_ids = {d["active_version_id"] for d in documents if d["active_version_id"]}
    for d in documents:
        active = by_id.get(d["active_version_id"]) if d["active_version_id"] else None
        if d["active_version_id"] and active is None:
            report.add("active_version_missing", ERROR, f"{d['name']}: the active version does not exist",
                       document=d["name"])
        elif active and active["status"] != "READY":
            report.add("active_version_not_ready", ERROR,
                       f"{d['name']}: the active version v{active['version_number']} is {active['status']}, not READY",
                       document=d["name"], version=active["version_number"], status=active["status"])
        elif active and active["document_id"] != d["id"]:
            report.add("active_version_wrong_document", ERROR, f"{d['name']}: the active version belongs to another "
                       "document", document=d["name"])
    for v in versions:
        if v["status"] == "READY" and v["id"] not in active_ids:
            report.add("ready_version_not_active", ERROR,
                       f"{_name(doc_names, v)} is READY but is not its document's active version",
                       document=doc_names.get(v["document_id"]), version=v["version_number"])


def _qdrant_counts(qdrant, collection, report):
    """{version id: number of points}, from one exact facet over the whole collection."""
    if not qdrant.collection_exists(collection):
        report.add("collection_missing", ERROR, f"the Qdrant collection {collection!r} does not exist")
        return {}
    total = qdrant.count(collection, exact=True).count
    response = qdrant.facet(collection, key="version_id", limit=FACET_LIMIT, exact=True)
    counts = {hit.value: hit.count for hit in response.hits if hit.count > 0}  # exact facets list emptied values too
    report.checked["qdrant_points"] = total
    if len(response.hits) >= FACET_LIMIT:
        report.add("facet_truncated", WARNING,
                   f"more than {FACET_LIMIT} distinct version ids: the counts are incomplete")
    untagged = qdrant.count(
        collection, exact=True,
        count_filter=models.Filter(must=[models.IsEmptyCondition(is_empty=models.PayloadField(key="version_id"))]),
    ).count
    if untagged:
        report.add("untagged_points", WARNING, f"{untagged} point(s) carry no version_id (indexed before versioning "
                   "and not registered, or foreign)", points=untagged)
    return counts


def _check_chunks(v, counts, doc_names, report):
    expected, found = v["chunk_count"], counts.get(v["id"], 0)
    if expected is None:
        report.add("chunk_count_unknown", ERROR, f"{_name(doc_names, v)} is {v['status']} but has no chunk_count",
                   document=doc_names.get(v["document_id"]), version=v["version_number"])
    elif found != expected:
        what = "missing from the index" if found < expected else "more points than recorded"
        report.add("chunk_count_mismatch", ERROR,
                   f"{_name(doc_names, v)}: PostgreSQL says {expected} chunks, Qdrant holds {found} ({what})",
                   document=doc_names.get(v["document_id"]), version=v["version_number"], status=v["status"],
                   expected=expected, found=found)


def _check_payloads(v, qdrant, collection, doc_names, options, report):
    expected = v["chunk_count"] or 0
    version_filter = models.Filter(
        must=[models.FieldCondition(key="version_id", match=models.MatchValue(value=v["id"]))])
    seen, bad, offset = set(), Counter(), None
    while True:
        points, offset = qdrant.scroll(collection, scroll_filter=version_filter, limit=options.batch, offset=offset,
                                       with_payload=PAYLOAD_KEYS, with_vectors=False)
        for point in points:
            payload = point.payload or {}
            if payload.get("document_id") != v["document_id"]:
                bad["document_id"] += 1
            if v["file_hash"] and payload.get("file_hash") != v["file_hash"]:
                bad["file_hash"] += 1
            if payload.get("chunks_total") != expected:
                bad["chunks_total"] += 1
            index = payload.get("chunk_index")
            if not isinstance(index, int) or not 0 <= index < expected:
                bad["chunk_index"] += 1
            elif index in seen:
                bad["duplicate_chunk_index"] += 1
            else:
                seen.add(index)
        if offset is None:
            break
    report.checked["points_scanned"] += sum(1 for _ in seen) + sum(bad.values())
    if bad:
        report.add("payload_invalid", ERROR, f"{_name(doc_names, v)}: points with wrong identifiers: {dict(bad)}",
                   document=doc_names.get(v["document_id"]), version=v["version_number"], problems=dict(bad))


def _check_file(v, storage_dir, doc_names, options, report, hash_it):
    path = _resolve(storage_dir, v["file_path"])
    subject = {"document": doc_names.get(v["document_id"]), "version": v["version_number"], "path": v["file_path"]}
    if path is None:
        report.add("file_path_outside_storage", ERROR, f"{_name(doc_names, v)}: the recorded path leaves the storage "
                   "folder", **subject)
    elif not path.is_file():
        report.add("missing_file", ERROR, f"{_name(doc_names, v)} ({v['status']}): the stored file is missing",
                   **subject)
    elif hash_it:
        actual = sha256_of(path)
        report.checked["files_hashed"] += 1
        if v["file_hash"] is None:
            report.add("hash_unknown", WARNING, f"{_name(doc_names, v)}: no file hash recorded to compare with",
                       **subject)
        elif actual != v["file_hash"]:
            report.add("hash_mismatch", ERROR, f"{_name(doc_names, v)}: the stored file is not the uploaded one "
                       "(SHA-256 differs)", **subject)


def _orphan_points(counts, by_id, doc_names, report):
    for version_id, points in counts.items():
        v = by_id.get(version_id)
        if v is None:
            report.add("orphan_points", WARNING, f"{points} point(s) of version {version_id} that PostgreSQL does "
                       "not know", version_id=version_id, points=points)
        elif v["status"] in ("FAILED", "UPLOADED"):
            report.add("failed_version_points", WARNING, f"{_name(doc_names, v)} is {v['status']} but still has "
                       f"{points} point(s) in the index", document=doc_names.get(v["document_id"]),
                       version=v["version_number"], points=points)


def _orphan_files(versions, storage_dir, report):
    root = Path(storage_dir)
    if not root.is_dir():
        return
    referenced = {v["file_path"].replace("\\", "/") for v in versions}
    for folder, dirs, files in os.walk(root):
        if Path(folder) == root and ".uploading" in dirs:
            dirs.remove(".uploading")  # uploads in progress are not orphans
        for name in files:
            full = Path(folder) / name
            relative = full.relative_to(root).as_posix()
            report.checked["storage_files"] += 1
            if relative not in referenced:
                report.add("orphan_file", WARNING, f"a stored file that no version refers to: {relative}",
                           path=relative, bytes=full.stat().st_size)


def run(target=None, options=None):
    """Reconcile one target and return the Report. Nothing is changed in PostgreSQL, Qdrant or the storage folder."""
    target, options = target or Target.from_config(), options or Options()
    report = Report(target, options)
    state = load_state(target)
    check(state, ReadOnlyQdrant(target.qdrant()), target.collection, target.storage_dir, options, report)
    report.finished_at = datetime.now(timezone.utc)
    return report


# ---------- the last result, for /api/status ----------

def save_last(report, directory=None):
    """Keep the last report for the status page: atomically replaced, outside PostgreSQL, Qdrant and storage."""
    directory = Path(directory or config.REPORT_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    temp = directory / f".{REPORT_NAME}.tmp"
    temp.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, directory / REPORT_NAME)


def load_last(directory=None):
    """{finished_at, age_seconds, ok, errors, warnings, by_code} of the last saved report, or None."""
    path = Path(directory or config.REPORT_DIR) / REPORT_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        finished = datetime.fromisoformat(data["finished_at"])
        summary = data["summary"]
        return {"finished_at": data["finished_at"], "ok": summary["ok"], "errors": summary["errors"],
                "warnings": summary["warnings"], "by_code": {c: v["count"] for c, v in summary["by_code"].items()},
                "age_seconds": int((datetime.now(timezone.utc) - finished).total_seconds())}
    except (OSError, ValueError, KeyError):
        return None


# ---------- command line ----------

def render(report):
    data = report.to_dict()
    seen = data["checked"]
    lines = [f"reconcile {data['started_at']}  ({data['duration_seconds']} s)",
             f"  PostgreSQL {seen.get('documents', 0)} documents, {seen.get('versions', 0)} versions; "
             f"Qdrant {seen.get('qdrant_points', 0)} points; storage {seen.get('storage_files', 0)} files",
             f"  checked deeply: {seen.get('versions_checked_deeply', 0)} versions, "
             f"{seen.get('files_hashed', 0)} files hashed"]
    for issue in report.issues:
        lines.append(f"  [{issue['severity'].upper():7}] {issue['code']}: {issue['message']}")
    for code, entry in data["summary"]["by_code"].items():
        if entry["count"] > report.options.example_limit:
            lines.append(f"  ... {code}: {entry['count']} in all, the first {report.options.example_limit} are listed")
    verdict = "CLEAN" if report.exit_code == 0 else f"{report.errors} error(s), {report.warnings} warning(s)"
    lines.append(f"result: {verdict}")
    return "\n".join(lines)


def main(argv):
    parser = argparse.ArgumentParser(prog="python -m rag reconcile", description="Read-only check of PostgreSQL, "
                                     "Qdrant and the stored files against each other.")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    parser.add_argument("--no-hash", action="store_true", help="do not read the files to compare SHA-256")
    parser.add_argument("--quick", action="store_true",
                        help="do not scan every point's identifiers (counts and files only)")
    parser.add_argument("--include-superseded", action="store_true", help="check the versions kept for rollback too")
    parser.add_argument("--no-save", action="store_true", help="do not keep this report for /api/status")
    args = parser.parse_args(argv)
    options = Options(hash_files=not args.no_hash, scan_payloads=not args.quick,
                      include_superseded=args.include_superseded)
    started = time.monotonic()
    try:
        report = run(options=options)
    except Exception as error:  # noqa: BLE001 - the command must say it could not check, with a clear exit code
        print(f"reconcile could not run: {type(error).__name__}", file=sys.stderr)
        log.debug("reconcile failed", exc_info=True)
        return 3
    if not args.no_save:
        save_last(report)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) if args.json else render(report))
    log.info("reconcile finished in %.1f s, exit code %d", time.monotonic() - started, report.exit_code)
    return report.exit_code
