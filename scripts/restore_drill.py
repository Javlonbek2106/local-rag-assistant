"""Restore drill: prove, on a clean environment, that a backup brings the whole system back, and time it.

    python scripts/restore_drill.py [--keep] [--json PATH]

Everything happens in throw-away Docker containers on free loopback ports and temp folders (tests/stack.py). The running
system (the compose stack, its storage, its documents) is never read or written.

  1. SOURCE   an empty stack is filled through the REAL pipeline (Docling, Tesseract, bge-m3 via Ollama, real worker)
              with the sample documents of tests/fixtures; one document gets a second version, a rollback and a
              roll-forward, so version history and an activation generation exist.
  2. BACKUP   `python -m rag backup` (timed), verified.
  3. CHANGE   one more document is added to the source AFTER the backup: it must NOT be in the restored system. That
              shows what the recovery point really is.
  4. RESTORE  into a second, EMPTY stack (new PostgreSQL, new Qdrant, empty storage folder), timed per phase;
              `reconcile` must be clean.
  5. START    the real application (`python -m rag api`) is started on the restored data; time to /readyz.
  6. CHECK    the same sample questions as the integration tests: the right file on top and the fact in it, against the
              source baseline; two full answers through the API; version history, generation and a rollback on the
              restored system; the post-backup document is absent.
RTO is measured from the start of the restore to the first successful search on the restored system. RPO is told from
what the backup actually contains. Needs Docker, Ollama (bge-m3 and the answer model), Tesseract and the parsing models.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from rag import answer, backup, config, db, ingest, llm, reconcile, store  # noqa: E402
from rag.backup import PgTools  # noqa: E402
from rag.target import Target  # noqa: E402
from rag.worker import Worker  # noqa: E402
from tests import stack as stack_tools  # noqa: E402
from tests.fixtures.make_samples import CASES, make_samples  # noqa: E402

COLLECTION = "documents_v2"


def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def point_config_at(target):
    """The in-process application code (db, store, worker) reads config; aim it at one environment."""
    config.DATABASE_URL, config.DB_SCHEMA = target.database_url, None
    config.QDRANT_URL, config.COLLECTION, config.STORAGE_DIR = target.qdrant_url, target.collection, target.storage_dir
    db.close()
    db.pool.cache_clear()
    store.get_client.cache_clear()


def index_everything(worker, expected, timeout=900):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not worker.run_once():
            documents = {d["name"]: d for d in db.list_documents()}
            if all(documents.get(name, {}).get("state") == "done" for name in expected):
                return
            time.sleep(1)
    raise SystemExit("the worker did not finish indexing in time")


def upload(path, content=None):
    temp = Path(tempfile.mkdtemp()) / path.name
    temp.write_bytes(content if content is not None else path.read_bytes())
    return ingest.queue_file(path.name, temp, temp.stat().st_size)


def search_results():
    """For each sample question: (file of the best hit, the fact is in it). The same check as the integration tests."""
    results = {}
    for question, file_name, fact in CASES:
        hits = answer.retrieve(question)
        top = hits[0].payload if hits else {}
        results[file_name] = (top.get("file_name"), fact.lower() in (top.get("text") or "").lower())
    return results


def ask_api(url, question):
    request = urllib.request.Request(f"{url}/api/ask", data=json.dumps({"question": question}).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=600) as response:
        events = [json.loads(line) for line in response.read().decode().splitlines() if line]
    return events[-1]["final"]


def stop_process(process):
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
    else:
        process.terminate()
    process.wait(timeout=30)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="keep the containers and folders for inspection")
    parser.add_argument("--json", type=Path, help="also write the summary to this file")
    args = parser.parse_args()
    if not stack_tools.docker_available():
        sys.exit("Docker is not available")

    work = Path(tempfile.mkdtemp(prefix="rag-drill-"))
    source_stack, target_stack = stack_tools.Stack("src"), stack_tools.Stack("dst")
    api = None
    summary = {"environment": {"python": sys.version.split()[0], "platform": sys.platform,
                               "cpu_count": os.cpu_count(), "qdrant": stack_tools.Stack.QDRANT_IMAGE,
                               "postgres": stack_tools.Stack.PG_IMAGE}}
    try:
        t0 = time.time()
        log("starting two empty stacks (source and restore target)")
        source_stack.start()
        target_stack.start()
        summary["stacks_started_seconds"] = round(time.time() - t0, 1)

        # ---------- 1. the source system, built by the real pipeline ----------
        source = Target(source_stack.database_url, source_stack.qdrant_url, COLLECTION, work / "source-storage")
        point_config_at(source)
        db.migrate()
        store.ensure_collection(store.get_client(), lambda: len(llm.embed(["probe"])[0]))
        samples = make_samples(work / "samples")
        names = sorted(p.name for p in samples.iterdir() if ingest.is_supported(p))
        for name in names:
            upload(samples / name)
        worker = Worker(worker_id="drill")
        worker.client = store.get_client()
        log("loading the parsing models and indexing the sample documents (real pipeline)")
        worker.converter, worker.chunker = ingest.build_pipeline()
        t0 = time.time()
        index_everything(worker, names)
        summary["source_indexing_seconds"] = round(time.time() - t0, 1)

        wifi = samples / "08_wifi.txt"
        upload(wifi, b"O'quv markazida Wi-Fi nomi Lola-6G, parol esa 4455.")  # a second version of one document
        index_everything(worker, names)
        first, second = db.get_version("08_wifi.txt", 1), db.get_version("08_wifi.txt", 2)
        db.switch_active(first["id"])  # a rollback ...
        db.switch_active(second["id"])  # ... and a roll-forward: the activation generation is now 2
        baseline = search_results()
        facts = db.status_snapshot()
        summary["source"] = {"documents": facts["documents"]["total"], "versions": facts["versions"]["total"],
                             "points": store.get_client().count(COLLECTION, exact=True).count,
                             "baseline_top_file_and_fact": {k: list(v) for k, v in baseline.items()}}
        log(f"source ready: {summary['source']['documents']} documents, {summary['source']['versions']} versions, "
            f"{summary['source']['points']} points; reconcile: exit {reconcile.run(source).exit_code}")

        # ---------- 2. backup ----------
        t0 = time.time()
        folder = backup.backup(source, work / "backups", PgTools(container=source_stack.pg_container))
        summary["backup"] = {"seconds": round(time.time() - t0, 2), "folder": folder.name,
                             "valid": backup.verify(folder) == []}
        manifest = backup.read_manifest(folder)
        summary["backup"].update(
            postgres_dump_started_at=manifest["postgres"]["dump_started_at"],
            backup_finished_at=manifest["finished_at"],
            storage_files=manifest["storage"]["file_count"], storage_bytes=manifest["storage"]["total_bytes"],
            qdrant_points=manifest["qdrant"]["points_count"],
            size_bytes=sum(f.stat().st_size for f in folder.rglob("*") if f.is_file()))
        log(f"backup {folder.name}: {summary['backup']['seconds']} s, {summary['backup']['size_bytes'] / 1e6:.1f} MB")

        # ---------- 3. a change AFTER the backup (it must be lost: that is the recovery point) ----------
        late = work / "99_after_backup.txt"
        late.write_text("Zaxiradan keyin qo'shilgan hujjat: maxfiy kod QWERTY-7731.", encoding="utf-8")
        upload(late)
        index_everything(worker, [*names, late.name])
        summary["change_after_backup"] = {"document": late.name,
                                          "present_in_source": db.get_document(late.name) is not None}

        # ---------- 4. restore into the second, empty stack ----------
        target = Target(target_stack.database_url, target_stack.qdrant_url, COLLECTION, work / "restored-storage")
        t_restore = time.time()
        log("restoring into the empty target stack")
        restored = backup.restore(folder, target, PgTools(container=target_stack.pg_container))
        summary["restore"] = {**restored, "reconcile_clean": restored["ok"]}
        log(f"restore {restored['seconds']['total']} s, reconcile {restored['reconcile']}")

        # ---------- 5. start the real application on the restored data ----------
        port = stack_tools.free_port()
        env = {**os.environ, "RAG_DATABASE_URL": target.database_url, "RAG_QDRANT_URL": target.qdrant_url,
               "RAG_STORAGE": str(target.storage_dir), "RAG_COLLECTION": COLLECTION, "RAG_PORT": str(port),
               "PYTHONIOENCODING": "utf-8"}
        t_app = time.time()
        api = subprocess.Popen([sys.executable, "-m", "rag", "api"], env=env, cwd=ROOT, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
        url = f"http://127.0.0.1:{port}"
        while True:
            try:
                with urllib.request.urlopen(f"{url}/readyz", timeout=3) as response:
                    if response.status == 200:
                        break
            except Exception:  # noqa: BLE001
                pass
            if api.poll() is not None or time.time() - t_app > 180:
                raise SystemExit("the application did not become ready on the restored data")
            time.sleep(1)
        summary["app_ready_seconds"] = round(time.time() - t_app, 1)

        # ---------- 6. the first search on the restored system, and the checks ----------
        point_config_at(target)
        t_search = time.time()
        after = search_results()
        summary["first_search_seconds"] = round(time.time() - t_search, 2)
        summary["rto_seconds"] = round(time.time() - t_restore, 1)  # from the start of the restore to a working search
        summary["restored_matches_baseline"] = after == baseline
        summary["restored_top_file_and_fact"] = {k: list(v) for k, v in after.items()}

        qa = {question: ask_api(url, question)[:80] for question, _, _ in CASES[:2]}
        summary["qa_through_the_api"] = qa
        wifi_doc = db.get_document("08_wifi.txt")
        summary["history"] = {"active_version": wifi_doc["version"],
                              "activation_generation": wifi_doc["activation_generation"],
                              "versions": [(v["version_number"], v["status"]) for v in db.list_versions("08_wifi.txt")]}
        summary["post_backup_document_absent_after_restore"] = db.get_document(late.name) is None
        rollback = urllib.request.Request(f"{url}/api/documents/08_wifi.txt/rollback", method="POST",
                                          data=json.dumps({"version": 1}).encode(),
                                          headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(rollback, timeout=60) as response:
            summary["rollback_on_restored"] = json.load(response)
        final = reconcile.run(target, reconcile.Options(include_superseded=True))
        summary["final_reconcile_exit_code"] = final.exit_code

        # ---------- the recovery point, from what the backup holds ----------
        summary["rpo"] = {"restored_state_as_of": manifest["postgres"]["dump_started_at"],
                          "backup_finished_at": manifest["finished_at"],
                          "change_made_after_backup_is_lost": summary["post_backup_document_absent_after_restore"],
                          "rule": "recovery point = start of the PostgreSQL dump; worst case loss = the backup "
                                  "interval plus the backup duration"}
        ok = (summary["backup"]["valid"] and summary["restore"]["reconcile_clean"]
              and summary["restored_matches_baseline"] and summary["post_backup_document_absent_after_restore"]
              and summary["final_reconcile_exit_code"] == 0)
        summary["drill_passed"] = ok
        text = json.dumps(summary, indent=2, ensure_ascii=False, default=str)
        print(text)
        if args.json:
            args.json.write_text(text, encoding="utf-8")
        return 0 if ok else 1
    finally:
        db.close()
        if api:
            stop_process(api)
        if not args.keep:
            source_stack.stop()
            target_stack.stop()
            shutil.rmtree(work, ignore_errors=True)
        else:
            log(f"kept: {work}, containers {source_stack.pg_container}, {target_stack.pg_container}")


if __name__ == "__main__":
    sys.exit(main())
