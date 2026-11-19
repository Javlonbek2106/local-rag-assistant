"""/healthz, /readyz and /api/status: liveness is not readiness, nothing secret leaves, the status stays small."""
import json
import urllib.error

import pytest

from rag import config, health, reconcile, store

SECRET = "hunter2-very-secret"


@pytest.fixture(autouse=True)
def ollama_up(monkeypatch):
    """Ollama is not part of these unit tests: it answers fine unless a test says otherwise."""
    monkeypatch.setattr(health, "check_ollama", lambda: "ok")


@pytest.fixture
def broken_qdrant(monkeypatch):
    def refuse():
        raise ConnectionError(f"qdrant at http://user:{SECRET}@10.0.0.5:6333 refused the connection")

    monkeypatch.setattr(store, "get_client", refuse)


# ---------- liveness ----------

def test_healthz_says_ok_without_touching_any_dependency(client, repo, monkeypatch):
    repo.down = True
    monkeypatch.setattr(health, "check_postgres", lambda r: pytest.fail("liveness must not check PostgreSQL"))
    monkeypatch.setattr(health, "check_qdrant", lambda: pytest.fail("liveness must not check Qdrant"))
    monkeypatch.setattr(health, "check_ollama", lambda: pytest.fail("liveness must not check Ollama"))
    response = client.get("/healthz")
    assert response.status_code == 200 and response.json() == {"status": "ok"}


# ---------- readiness ----------

def test_readyz_is_ready_when_everything_answers(client):
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"postgres": "ok", "qdrant": "ok", "ollama": "ok"}}


def test_readyz_is_503_when_postgres_is_down_and_says_nothing_about_why(client, repo):
    repo.down = True
    response = client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready" and body["checks"]["postgres"] == "unreachable"
    assert SECRET not in response.text and "postgresql://" not in response.text and "Traceback" not in response.text


def test_readyz_is_503_when_qdrant_is_down(client, broken_qdrant):
    response = client.get("/readyz")
    assert response.status_code == 503 and response.json()["checks"]["qdrant"] == "unreachable"
    assert SECRET not in response.text and "10.0.0.5" not in response.text


def test_readyz_is_503_when_the_collection_is_missing(client, qdrant):
    qdrant.missing_collection = True
    response = client.get("/readyz")
    assert response.status_code == 503 and response.json()["checks"]["qdrant"] == "collection_missing"


def test_without_ollama_the_service_is_degraded_but_still_answers_200(client, monkeypatch):
    monkeypatch.setattr(health, "check_ollama", lambda: "unreachable")
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded" and response.json()["checks"]["ollama"] == "unreachable"


def test_liveness_and_readiness_differ_when_a_dependency_fails(client, repo):
    repo.down = True
    assert client.get("/healthz").status_code == 200  # the process is fine
    assert client.get("/readyz").status_code == 503  # but it cannot serve


# ---------- the Ollama check itself ----------

class FakeResponse:
    def __init__(self, models):
        self.body = json.dumps({"models": [{"name": m} for m in models]}).encode()

    def read(self, *args):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def real_check(monkeypatch):
    monkeypatch.undo()  # drop the autouse fake; this test needs the real function
    return health.check_ollama


def test_ollama_is_ok_with_both_models_installed(real_check, monkeypatch):
    names = [f"{config.EMBED_MODEL}:latest", config.LLM_MODEL]
    monkeypatch.setattr(health.urllib.request, "urlopen", lambda url, timeout: FakeResponse(names))
    assert health.check_ollama() == "ok"


def test_ollama_with_a_model_missing_is_reported(real_check, monkeypatch):
    monkeypatch.setattr(health.urllib.request, "urlopen", lambda url, timeout: FakeResponse([config.LLM_MODEL]))
    assert health.check_ollama() == "models_missing"


def test_ollama_that_does_not_answer_is_unreachable(real_check, monkeypatch):
    def refuse(url, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(health.urllib.request, "urlopen", refuse)
    assert health.check_ollama() == "unreachable"


@pytest.mark.parametrize("host, expected", [(None, "http://127.0.0.1:11434"), ("10.1.2.3", "http://10.1.2.3:11434"),
                                            ("http://box:9999/", "http://box:9999"), ("box:8000", "http://box:8000")])
def test_the_ollama_address_follows_ollama_host(monkeypatch, host, expected):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    if host:
        monkeypatch.setenv("OLLAMA_HOST", host)
    assert health.ollama_base() == expected


# ---------- status ----------

def test_status_summarises_documents_queue_stage_and_errors(client, repo):
    for name in ("a.txt", "b.txt", "c.txt", "d.txt"):
        repo.create_version(name, 5, name)
    repo.set_status("a.txt", 1, "READY", chunks=3, active=True)
    repo.set_status("b.txt", 1, "QUEUED")
    repo.set_status("c.txt", 1, "EMBEDDING")
    repo.set_status("d.txt", 1, "FAILED", error="Vaqt tugadi (30 daqiqa)")
    body = client.get("/api/status").json()
    assert body["ready"] == "ready" and body["dependencies"] == {"postgres": "ok", "qdrant": "ok", "ollama": "ok"}
    assert body["documents"]["total"] == 4 and body["documents"]["searchable"] == 1
    assert body["versions"]["by_status"] == {"READY": 1, "QUEUED": 1, "EMBEDDING": 1, "FAILED": 1}
    queue = body["queue"]
    assert queue["queued"] == 1 and queue["oldest_queued_age_seconds"] == 30 and queue["processing"] == 1
    (running,) = queue["in_flight"]
    assert (running["document"], running["stage"], running["progress"]) == ("c.txt", "EMBEDDING", None)
    assert body["last_errors"] == [{"document": "d.txt", "version": 1, "status": "FAILED",
                                    "error": "Vaqt tugadi (30 daqiqa)", "at": None}]


def test_status_is_small_however_many_documents_there_are(client, repo):
    for i in range(300):
        repo.create_version(f"doc{i}.txt", 1, f"h{i}")
    body = client.get("/api/status").json()
    assert body["documents"]["total"] == 300 and "name" not in json.dumps(body["documents"])
    assert len(client.get("/api/status").content) < 4000  # counts, never the list


def test_status_hides_secrets_in_stored_error_texts(client, repo):
    repo.create_version("a.txt", 1, "h")
    repo.set_status("a.txt", 1, "FAILED", error=f"connect failed: postgresql://rag:{SECRET}@db/rag password={SECRET}")
    response = client.get("/api/status")
    assert SECRET not in response.text
    assert "***" in response.json()["last_errors"][0]["error"]


def test_status_still_answers_when_postgres_is_down(client, repo):
    repo.down = True
    response = client.get("/api/status")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] == "not_ready" and body["dependencies"]["postgres"] == "unreachable"
    assert body["documents"] is None and body["queue"] is None and SECRET not in response.text


def test_status_shows_the_last_reconciliation_when_there_is_one(client, monkeypatch, tmp_path):
    folder = tmp_path / "reports"
    folder.mkdir()
    (folder / reconcile.REPORT_NAME).write_text(json.dumps({
        "finished_at": "2026-11-13T09:00:00+00:00",
        "summary": {"ok": False, "errors": 2, "warnings": 1,
                    "by_code": {"chunk_count_mismatch": {"severity": "error", "count": 2},
                                "orphan_file": {"severity": "warning", "count": 1}}}}), encoding="utf-8")
    monkeypatch.setattr(config, "REPORT_DIR", folder)
    last = client.get("/api/status").json()["reconcile"]
    assert last["finished_at"] == "2026-11-13T09:00:00+00:00" and last["ok"] is False
    assert last["errors"] == 2 and last["by_code"]["chunk_count_mismatch"] == 2 and last["age_seconds"] > 0


def test_status_has_no_reconcile_entry_before_the_first_run(client, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "REPORT_DIR", tmp_path / "none")
    assert client.get("/api/status").json()["reconcile"] is None


# ---------- the existing protection covers the new endpoints ----------

@pytest.mark.parametrize("path", ["/healthz", "/readyz", "/api/status"])
def test_a_foreign_host_is_refused_and_the_security_headers_are_set(client, path):
    assert client.get(path, headers={"host": "evil.com"}).status_code == 403
    response = client.get(path)
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"


# ---------- redaction ----------

@pytest.mark.parametrize("text, forbidden", [
    ("postgresql://rag:pw123@host/db failed", "pw123"),
    ("auth failed, password=pw123 rejected", "pw123"),
    ("API_KEY: pw123", "pw123"),
    ("token = pw123 expired", "pw123"),
])
def test_redact_removes_credentials(text, forbidden):
    assert forbidden not in health.redact(text) and "***" in health.redact(text)


def test_redact_makes_one_short_line():
    out = health.redact("line one\nline two\n" + "x" * 1000)
    assert "\n" not in out and len(out) <= 300
