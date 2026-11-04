"""Background indexing: one file at a time, status kept per file name.

Status lives in memory; the folder on disk and the index are the durable truth, and both are
reconciled at startup. Making this survive restarts mid-job and enforce time limits needs a database
and a separate process (plan, stage 2).
"""
import logging
import queue
import threading
import time

from rag import config, ingest, llm, store

log = logging.getLogger(__name__)


def list_files():
    if not config.DOCS_DIR.exists():
        return []
    return sorted(p for p in config.DOCS_DIR.iterdir() if p.is_file() and ingest.is_supported(p))


def purge_stale(client):
    """Drop index entries whose file is gone from the documents folder. Returns how many sources were dropped."""
    present = {str(p) for p in list_files()}
    stale = [source for source in store.indexed_sources(client) if source not in present]
    if stale and not present:
        # An empty folder is more likely a missing drive or a wrong path than a deliberate wipe.
        log.warning("documents folder %s has no files but the index has %d; not purging", config.DOCS_DIR, len(stale))
        return 0
    for source in stale:
        store.delete_source(client, source)
        log.info("purged index entries of missing file %s", source)
    return len(stale)


class IndexWorker:
    def __init__(self):
        self._jobs = queue.Queue()
        self._status = {}
        self._lock = threading.Lock()
        self.error = None

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="index-worker").start()

    def submit(self, name):
        self._set(name, "queued")
        self._jobs.put(name)

    def is_processing(self, name):
        with self._lock:
            return self._status.get(name, {}).get("state") == "processing"

    def forget(self, name):
        with self._lock:
            self._status.pop(name, None)

    def queued(self):
        return self._jobs.qsize()

    def snapshot(self):
        now = time.time()
        with self._lock:
            result = {}
            for name, info in self._status.items():
                info = dict(info)
                started = info.pop("started_at", None)
                info["seconds"] = round(now - started) if started and info["state"] == "processing" else None
                result[name] = info
            return result

    def _set(self, name, state, chunks=None, error=None, started_at=None):
        with self._lock:
            self._status[name] = {"state": state, "chunks": chunks, "error": error, "started_at": started_at}

    def _run(self):
        try:
            client = store.get_client()
            store.ensure_collection(client, lambda: len(llm.embed(["probe"])[0]))
            purge_stale(client)
            converter, chunker = ingest.build_pipeline()
        except Exception as error:  # Qdrant or Ollama not running, Tesseract missing, ...
            log.exception("index worker failed to start")
            self.error = f"Indekslash xizmati ishga tushmadi: {error}"
            return
        log.info("index worker ready")
        while True:
            self._process(client, converter, chunker, self._jobs.get())

    def _process(self, client, converter, chunker, name):
        path = config.DOCS_DIR / name
        if not path.exists():  # deleted while waiting in the queue
            return
        started = time.time()
        self._set(name, "processing", started_at=started)
        try:
            count = ingest.index_file(client, converter, chunker, path)
            if not path.exists():  # deleted while being indexed: drop what was just written
                store.delete_source(client, str(path))
                self.forget(name)
                return
            chunks = store.count_chunks(client, str(path)) if count is None else count
            self._set(name, "done", chunks=chunks)
            outcome = "unchanged" if count is None else f"{chunks} chunks"
            log.info("%s: %s in %.1fs", name, outcome, time.time() - started)
        except Exception as error:
            log.exception("indexing %s failed", name)
            self._set(name, "failed", error=str(error)[:300])
