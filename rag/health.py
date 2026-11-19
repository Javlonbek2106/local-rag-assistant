"""Liveness, readiness and the compact status of the running system.

- /healthz  only says the process answers. It touches no dependency, so a database or Qdrant outage can never make a
  liveness probe fail and get a healthy process restarted for nothing.
- /readyz   can this process serve requests? PostgreSQL and Qdrant are required (every request needs them); Ollama is
  needed to answer questions and to index but not to list, upload, delete or roll back, so without it the answer is
  "degraded" (still 200) and not "not_ready" (503).
- /api/status  a small summary (never the document list): counts, queue age, what is being indexed, the last errors,
  the dependency checks and the last reconciliation.

Everything returned is a status word or a number. Exceptions are logged, never sent: no connection string, password,
path of the server or stack trace can leave through these endpoints, and stored error texts are redacted first.
"""
import json
import logging
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from rag import config, llm, reconcile, store

log = logging.getLogger(__name__)
CHECK_TIMEOUT_S = 2
URL_CREDENTIALS = re.compile(r"://[^/\s@]*@")
SECRET_PAIR = re.compile(r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key)\b\s*[=:]\s*\S+")


def redact(text, limit=300):
    """An error text that is safe to show: credentials in URLs and secret-looking pairs removed, one line, short."""
    text = SECRET_PAIR.sub(lambda m: f"{m.group(1)}=***", URL_CREDENTIALS.sub("://***@", str(text or "")))
    return " ".join(text.split())[:limit]


def check_postgres(repo):
    try:
        repo.ping()
        return "ok"
    except Exception:  # noqa: BLE001 - the reason goes to the log, the caller only gets a status word
        log.warning("readiness: PostgreSQL check failed", exc_info=True)
        return "unreachable"


def check_qdrant():
    try:
        return "ok" if store.get_client().collection_exists(config.COLLECTION) else "collection_missing"
    except Exception:  # noqa: BLE001
        log.warning("readiness: Qdrant check failed", exc_info=True)
        return "unreachable"


ollama_base = llm.ollama_base  # one place that reads OLLAMA_HOST (rag/llm.py talks to the same server)


def check_ollama():
    """Ollama answers and has both configured models (embedding and answer)."""
    try:
        with urllib.request.urlopen(f"{ollama_base()}/api/tags", timeout=CHECK_TIMEOUT_S) as response:
            installed = {model["name"] for model in json.load(response).get("models", [])}
    except Exception:  # noqa: BLE001
        log.warning("readiness: Ollama check failed", exc_info=True)
        return "unreachable"
    wanted = (config.EMBED_MODEL, config.LLM_MODEL)
    missing = [m for m in wanted if m not in installed and f"{m}:latest" not in installed]
    return "ok" if not missing else "models_missing"


def dependencies(repo):
    """The three checks, run side by side so a slow one does not add to the others."""
    with ThreadPoolExecutor(max_workers=3) as pool:
        postgres, qdrant, ollama = (pool.submit(check_postgres, repo), pool.submit(check_qdrant),
                                    pool.submit(check_ollama))
        return {"postgres": postgres.result(), "qdrant": qdrant.result(), "ollama": ollama.result()}


def readiness(repo, connector=None):
    """`connector` is given only when the connector is enabled: a function that says "ok" or "unavailable". Without it
    the answer is exactly what it always was. With it, an unavailable connector means not ready (fail closed)."""
    checks = dependencies(repo)
    if connector is not None:
        checks["connector"] = connector()
    if checks["postgres"] != "ok" or checks["qdrant"] != "ok" or checks.get("connector", "ok") != "ok":
        status = "not_ready"
    elif checks["ollama"] != "ok":
        status = "degraded"
    else:
        status = "ready"
    return {"status": status, "checks": checks}


def _iso(moment):
    return moment.isoformat() if moment else None


def _in_flight(item):
    """One running version with its position as `progress` ({"unit", "done", "total", "age_seconds"}, or null when the
    worker has not reported one in the current stage); `age_seconds` is the time since the position last changed."""
    item = dict(item)
    unit, done = item.pop("progress_unit", None), item.pop("progress_done", None)
    total = item.pop("progress_total", None)
    age = item.pop("progress_age_seconds", None)
    item["progress"] = None if unit is None else {"unit": unit, "done": done, "total": total, "age_seconds": age}
    return item


def status_report(repo, connector=None):
    """The compact status. If PostgreSQL itself is down the numbers are missing, the dependency checks still answer.
    `connector` is given only when the connector is enabled (see `readiness`)."""
    checks = dependencies(repo)
    if connector is not None:
        checks["connector"] = connector()
    snapshot = None
    if checks["postgres"] == "ok":
        try:
            snapshot = repo.status_snapshot()
        except Exception:  # noqa: BLE001
            log.warning("status: reading the database failed", exc_info=True)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ready": "not_ready" if checks["postgres"] != "ok" or checks["qdrant"] != "ok"
        or checks.get("connector", "ok") != "ok" else ("ready" if checks["ollama"] == "ok" else "degraded"),
        "dependencies": checks,
        "documents": None, "versions": None, "queue": None, "last_errors": None,
        "reconcile": reconcile.load_last(),
    }
    if snapshot:
        in_flight_total = sum(snapshot["versions"]["by_status"].get(s, 0) for s in snapshot["in_flight_statuses"])
        report["documents"] = snapshot["documents"]
        report["versions"] = snapshot["versions"]
        report["queue"] = {
            "queued": snapshot["queued"],
            "oldest_queued_age_seconds": snapshot["oldest_queued_age_seconds"],
            "processing": in_flight_total,
            "in_flight": [_in_flight(item) for item in snapshot["in_flight"]],
        }
        report["last_errors"] = [dict(item, error=redact(item["error"]), at=_iso(item["at"]))
                                 for item in snapshot["last_errors"]]
    return report
