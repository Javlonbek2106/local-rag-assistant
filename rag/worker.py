"""Indexing worker: runs as its own process, claims documents from PostgreSQL and indexes them one at a time.

    python -m rag.worker      one worker (loads the parsing models, then loops)
    python -m rag worker      the same under a supervisor that restarts it

While a document is processed a guard thread sends heartbeats and enforces the time limit. A stuck parser cannot
be killed from inside a Python process, so on timeout the worker exits with EXIT_RESTART and the supervisor starts
a fresh one. If the whole process dies, its documents are put back in the queue by whichever worker is alive.
"""
import hashlib
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import uuid

from rag import config, db, ingest, llm, storage, store, versioning

log = logging.getLogger(__name__)
EXIT_RESTART = 75


class ClaimLost(Exception):
    """This worker was declared dead and replaced while it was working; it must stop and write nothing."""


def purge_orphans(client, known_ids):
    """Drop index entries that belong to no document row (left over after a crash in the middle of a delete).

    Refuses when no document is known at all: that looks like a wrong or empty database, not a deliberate wipe.
    """
    orphans = [source for source in store.indexed_sources(client) if source not in known_ids]
    if orphans and not known_ids:
        log.warning("the database lists no documents but the index holds %d; not purging", len(orphans))
        return 0
    for source in orphans:
        store.delete_source(client, source)
        log.info("purged index entries of unknown document %s", source)
    return len(orphans)


class JobGuard:
    """While one document is processed: heartbeats, loss-of-claim detection and the time limit."""

    def __init__(self, repo, job, worker_id, on_timeout):
        self.repo, self.job, self.worker_id, self.on_timeout = repo, job, worker_id, on_timeout
        self.lost = False
        self.timed_out = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="job-guard")

    def __enter__(self):
        self._started = time.monotonic()
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self):
        while not self._stop.wait(config.JOB_HEARTBEAT_S):
            if time.monotonic() - self._started > config.JOB_TIMEOUT_S:
                self.timed_out = True
                limit = f"{config.JOB_TIMEOUT_S / 60:g} daqiqa"
                log.error("%s exceeded the time limit (%s)", self.job["name"], limit)
                self.repo.fail(self.job["id"], self.worker_id, f"Vaqt tugadi ({limit})", retry=False)
                self.on_timeout()
                return
            try:
                if not self.repo.heartbeat(self.job["id"], self.worker_id):
                    self.lost = True
                    log.warning("%s: claim lost, another worker has taken over", self.job["name"])
                    return
            except Exception:  # noqa: BLE001 - a database blip must not stop the guard; stale detection covers real loss
                log.exception("heartbeat failed")


class Worker:
    def __init__(self, repo=db, worker_id=None, on_timeout=None):
        self.repo = repo
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.on_timeout = on_timeout or (lambda: os._exit(EXIT_RESTART))
        self.client = self.converter = self.chunker = None
        self._last_reap = 0.0

    def setup(self):
        self.repo.migrate()
        self.client = store.get_client()
        store.ensure_collection(self.client, lambda: len(llm.embed(["probe"])[0]))
        versioning.backfill_legacy(self.repo, self.client)
        self.converter, self.chunker = ingest.build_pipeline()
        purge_orphans(self.client, {str(d["id"]) for d in self.repo.list_documents()})
        log.info("worker %s ready", self.worker_id)

    def run_forever(self):
        self.setup()
        while True:
            if not self.run_once():
                time.sleep(config.WORKER_POLL_S)

    def run_once(self):
        """Take back dead workers' documents (at most every few seconds), then process one document if any."""
        if time.monotonic() - self._last_reap > config.JOB_HEARTBEAT_S:
            self._last_reap = time.monotonic()
            self.repo.requeue_stale(config.JOB_STALE_AFTER_S)
        job = self.repo.claim_next(self.worker_id)
        if job is None:
            return False
        self.process(job)
        return True

    def index(self, job):
        """Parse, embed, index and verify one version; return its chunk count. Nothing here makes it active."""
        path = storage.resolve(job["file_path"])
        if not path.exists():
            raise FileNotFoundError(f"fayl topilmadi: {job['name']}")
        file_hash = job["file_hash"]
        if file_hash is None:  # a version registered by the migration that was queued again before the backfill
            with path.open("rb") as f:
                file_hash = hashlib.file_digest(f, "sha256").hexdigest()
            self.repo.set_file_hash(job["id"], file_hash)

        def stage(status):
            if not self.repo.set_stage(job["id"], self.worker_id, status):
                raise ClaimLost(job["name"])

        count = ingest.index_version(self.client, self.converter, self.chunker, path, document_id=job["document_id"],
                                     version_id=job["id"], file_name=job["file_name"], file_hash=file_hash,
                                     stage=stage)
        stage("VERIFYING")
        versioning.verify(self.client, job["document_id"], job["id"], file_hash, count)
        return count

    def process(self, job):
        started = time.monotonic()
        with JobGuard(self.repo, job, self.worker_id, self.on_timeout) as guard:
            try:
                count = self.index(job)
            except ClaimLost:
                log.warning("%s: claim lost, another worker has taken over; stopping", job["name"])
                outcome = ("lost", None, None)
            except (ingest.UnreadableDocument, FileNotFoundError, ValueError) as error:
                log.warning("%s: %s", job["name"], error)
                outcome = ("fail", str(error), False)
            except Exception as error:  # noqa: BLE001 - anything else may be temporary (Qdrant, Ollama): retry
                log.exception("indexing %s failed (attempt %d)", job["name"], job["attempts"])
                outcome = ("fail", str(error), True)
            else:
                outcome = ("activate", count, None)
        if guard.timed_out:
            return  # already recorded by the guard, which is about to restart the process
        kind, value, retry = outcome
        if kind == "activate":
            result = self.repo.activate(job["id"], self.worker_id, value)
            if result:
                log.info("%s v%d: %d chunks in %.1fs, %s", job["name"], job["version_number"], value,
                         time.monotonic() - started, result)
            else:
                log.warning("%s finished, but another worker already owns it; result discarded", job["name"])
        elif kind == "fail":
            state = self.repo.fail(job["id"], self.worker_id, value, retry=retry)
            log.info("%s -> %s", job["name"], state)


class Supervisor:
    """Keeps one worker process alive and stops it cleanly with its owner."""

    def __init__(self):
        self._stop = threading.Event()
        self._process = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="worker-supervisor")

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._thread.join(timeout=5)

    def join(self):
        while self._thread.is_alive():
            self._thread.join(timeout=0.5)

    def _run(self):
        while not self._stop.is_set():
            self._process = subprocess.Popen([sys.executable, "-m", "rag.worker"])
            code = self._process.wait()
            if self._stop.is_set():
                return
            log.warning("worker exited with code %s, restarting", code)
            self._stop.wait(1 if code == EXIT_RESTART else 5)  # back off so a crash loop does not spin


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    try:
        Worker().run_forever()
    except KeyboardInterrupt:
        pass
