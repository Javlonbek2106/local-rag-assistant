"""Reconciliation, readiness and status against a real PostgreSQL."""
import hashlib

import psycopg
import pytest
from fastapi.testclient import TestClient

from rag import config, db, health, reconcile, storage, store
from rag.api import create_app
from rag.reconcile import Options, ReadOnlyQdrant, Report
from rag.target import Target
from tests.conftest import FakeQdrant

pytestmark = pytest.mark.db
COLLECTION = "documents_v2"


def target(tmp_path):
    return Target(config.DATABASE_URL, "http://127.0.0.1:1", COLLECTION, tmp_path / "storage", config.DB_SCHEMA)


class Site:
    """Real rows in the test schema, real files in a temp storage, vectors in an in-memory Qdrant."""

    def __init__(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "STORAGE_DIR", tmp_path / "storage")
        self.target = target(tmp_path)
        self.qdrant = FakeQdrant()

    def publish(self, name, content, chunks=2):
        """Upload, index and activate a version the way the system does, with its file and its points in place."""
        data = content.encode()
        created = db.create_version(name, len(data), hashlib.sha256(data).hexdigest())
        path = storage.resolve(created["file_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        db.enqueue(created["id"])
        job = db.claim_next("w1")
        while job["id"] != created["id"]:  # another test document may be waiting first
            job = db.claim_next("w1")
        for i in range(chunks):
            self.qdrant.points[f"{created['id']}-{i}"] = ([0.1], {
                "document_id": str(created["document_id"]), "version_id": str(created["id"]),
                "file_hash": hashlib.sha256(data).hexdigest(), "chunks_total": chunks, "chunk_index": i,
                "source": str(created["document_id"])})
        assert db.set_stage(job["id"], "w1", "VERIFYING") and db.record_verified(job["id"], "w1", chunks)
        assert db.activate(job["id"], "w1", chunks) == "activated"
        return created

    def reconcile(self, **options):
        state = reconcile.load_state(self.target)
        report = Report(self.target, Options(**options))
        reconcile.check(state, ReadOnlyQdrant(self.qdrant), COLLECTION, self.target.storage_dir, report.options,
                        report)
        return report


@pytest.fixture
def site(tmp_path, monkeypatch):
    return Site(tmp_path, monkeypatch)


def table_contents():
    """Every row of every table of the schema, as comparable tuples: the whole PostgreSQL state."""
    with db.pool().connection() as conn:
        tables = [r["tablename"] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() ORDER BY 1")]
        return {t: sorted(map(str, (tuple(r.values()) for r in conn.execute(f"SELECT * FROM {t}")))) for t in tables}


# ---------- a real system is clean, and damage is found ----------

def test_a_system_built_by_the_real_code_reconciles_clean(site):
    site.publish("a.txt", "birinchi")
    site.publish("a.txt", "ikkinchi")  # version 1 is kept for rollback
    site.publish("b.txt", "uchinchi")
    report = site.reconcile(include_superseded=True)
    assert report.issues == [] and report.exit_code == 0
    checked = report.to_dict()["checked"]
    assert checked["documents"] == 2 and checked["versions"] == 3 and checked["versions_checked_deeply"] == 3
    assert checked["files_hashed"] == 3 and checked["qdrant_points"] == 6


def test_a_vector_removed_from_the_index_is_found_against_the_real_rows(site):
    created = site.publish("a.txt", "matn", chunks=3)
    del site.qdrant.points[f"{created['id']}-1"]
    report = site.reconcile()
    (issue,) = report.issues
    assert issue["code"] == "chunk_count_mismatch" and issue["subject"]["document"] == "a.txt"
    assert (issue["subject"]["expected"], issue["subject"]["found"]) == (3, 2) and report.exit_code == 1


def test_a_deleted_and_a_changed_file_are_found_against_the_real_rows(site):
    one, two = site.publish("a.txt", "bir"), site.publish("b.txt", "ikki")
    storage.resolve(one["file_path"]).unlink()
    storage.resolve(two["file_path"]).write_bytes(b"changed")
    assert {i["code"] for i in site.reconcile().issues} == {"missing_file", "hash_mismatch"}


def test_orphans_on_both_sides_are_found_against_the_real_rows(site):
    site.publish("a.txt", "matn")
    site.qdrant.points["ghost"] = ([0.1], {"version_id": "11111111-1111-1111-1111-111111111111"})
    stray = site.target.storage_dir / "documents" / "gone" / "original.pdf"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"x")
    report = site.reconcile()
    assert {i["code"] for i in report.issues} == {"orphan_points", "orphan_file"} and report.exit_code == 2


# ---------- read only: PostgreSQL is not touched ----------

def test_the_database_connection_reconcile_uses_is_read_only_and_a_write_on_it_fails(tmp_path):
    seen = {}
    real_connect = Target.connect

    def spy(self, **kwargs):
        conn = real_connect(self, **kwargs)
        seen["conn"] = conn
        return conn

    Target.connect = spy
    try:
        reconcile.load_state(target(tmp_path))
    finally:
        Target.connect = real_connect
    assert seen["conn"].read_only is True and seen["conn"].isolation_level == psycopg.IsolationLevel.REPEATABLE_READ
    with target(tmp_path).connect() as conn:  # the same setting makes the server itself refuse a write
        conn.read_only = True
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("UPDATE documents SET name = name")


def test_a_full_reconcile_leaves_every_row_of_every_table_exactly_as_it_was(site):
    created = site.publish("a.txt", "matn", chunks=3)
    site.publish("a.txt", "yangi matn")
    del site.qdrant.points[f"{created['id']}-0"]  # make it find something, so every branch runs
    storage.resolve(created["file_path"]).write_bytes(b"tampered")
    before = table_contents()
    report = site.reconcile(include_superseded=True)
    assert report.exit_code == 1 and report.issues
    assert table_contents() == before  # updated_at, status, active pointer, generations: nothing moved


def test_reconcile_does_not_requeue_activate_or_fail_anything(site):
    site.publish("a.txt", "bir")
    queued = db.create_version("a.txt", 3, "newhash")
    db.enqueue(queued["id"])
    stuck = db.create_version("c.txt", 3, "stuck")
    db.enqueue(stuck["id"])
    job = db.claim_next("w9")
    while job["id"] != stuck["id"]:  # the older queued version is taken first
        job = db.claim_next("w9")
    with db.pool().connection() as conn:
        conn.execute("UPDATE document_versions SET heartbeat_at = now() - interval '1 hour' WHERE id = %s",
                     (job["id"],))
    before = table_contents()
    site.reconcile()
    assert table_contents() == before and db.get_version("c.txt", 1)["status"] == "PROCESSING"  # not taken back


# ---------- status ----------

def test_status_snapshot_counts_ages_stages_and_errors(site):
    site.publish("ready.txt", "x")
    busy = db.create_version("busy.txt", 1, "b")
    db.enqueue(busy["id"])
    job = db.claim_next("w7")
    assert job["id"] == busy["id"] and db.set_stage(job["id"], "w7", "EMBEDDING")
    failed = db.create_version("failed.txt", 1, "f")
    db.enqueue(failed["id"])
    claimed = db.claim_next("w8")
    assert claimed["id"] == failed["id"]
    db.fail(failed["id"], "w8", "Qdrant to'xtadi", retry=False)
    queued = db.create_version("queued.txt", 1, "q")  # created last: nobody claimed it
    db.enqueue(queued["id"])
    with db.pool().connection() as conn:
        conn.execute("UPDATE document_versions SET updated_at = now() - interval '10 minutes' WHERE id = %s",
                     (queued["id"],))

    snapshot = db.status_snapshot()
    assert snapshot["documents"] == {"total": 4, "searchable": 1,
                                     "by_state": {"done": 1, "queued": 1, "processing": 1, "failed": 1}}
    assert snapshot["versions"]["by_status"] == {"READY": 1, "QUEUED": 1, "EMBEDDING": 1, "FAILED": 1}
    assert snapshot["queued"] == 1 and 590 <= snapshot["oldest_queued_age_seconds"] <= 700
    (running,) = snapshot["in_flight"]
    assert (running["document"], running["stage"], running["attempt"]) == ("busy.txt", "EMBEDDING", 1)
    assert running["running_seconds"] >= 0 and running["heartbeat_age_seconds"] >= 0
    (error,) = snapshot["last_errors"]
    assert (error["document"], error["status"], error["error"]) == ("failed.txt", "FAILED", "Qdrant to'xtadi")


def test_status_snapshot_stays_bounded_however_much_work_there_is(site):
    for i in range(30):
        created = db.create_version(f"w{i}.txt", 1, f"h{i}")
        db.enqueue(created["id"])
    with db.pool().connection() as conn:
        conn.execute("UPDATE document_versions SET status = 'PROCESSING', claimed_by = 'w', started_at = now(), "
                     "heartbeat_at = now() WHERE status = 'QUEUED'")
        conn.execute("UPDATE document_versions SET error_message = 'boom', status = 'FAILED' WHERE "
                     "version_number = 1 AND file_name LIKE 'w1%'")
    snapshot = db.status_snapshot()
    assert len(snapshot["in_flight"]) <= 20 and len(snapshot["last_errors"]) <= 10
    assert snapshot["documents"]["total"] == 30  # the counts still cover everything


def test_status_snapshot_of_an_empty_system(site):
    snapshot = db.status_snapshot()
    assert snapshot["documents"]["total"] == 0 and snapshot["queued"] == 0
    assert snapshot["oldest_queued_age_seconds"] is None
    assert snapshot["in_flight"] == [] and snapshot["last_errors"] == []


def test_ping_answers_on_a_working_database():
    assert db.ping() is None


# ---------- the endpoints on the real database ----------

@pytest.fixture
def api(storage_dir, monkeypatch):
    monkeypatch.setattr(store, "get_client", lambda: FakeQdrant())
    monkeypatch.setattr(store, "ensure_collection", lambda c, size: None)
    monkeypatch.setattr(health, "check_ollama", lambda: "ok")
    with TestClient(create_app(db), base_url=f"http://127.0.0.1:{config.PORT}") as client:
        yield client


def upload(client, name, content=b"matn"):
    return client.post("/api/documents", files=[("files", (name, content))]).json()["results"][0]


def test_readyz_and_status_work_on_the_real_database(api):
    upload(api, "a.txt")
    assert api.get("/healthz").json() == {"status": "ok"}
    assert api.get("/readyz").json()["checks"]["postgres"] == "ok"
    body = api.get("/api/status").json()
    assert body["documents"]["total"] == 1 and body["queue"]["queued"] == 1
    assert body["queue"]["oldest_queued_age_seconds"] >= 0


def test_a_database_that_stops_answering_makes_readyz_503_but_not_healthz(api, monkeypatch):
    def down():
        raise psycopg.OperationalError("connection to server at 10.0.0.9 failed: password authentication failed")

    monkeypatch.setattr(db, "ping", down)
    assert api.get("/healthz").status_code == 200
    response = api.get("/readyz")
    assert response.status_code == 503 and response.json()["checks"]["postgres"] == "unreachable"
    assert "10.0.0.9" not in response.text and "password" not in response.text


def test_the_upload_flow_still_works_with_the_new_endpoints_in_place(api):
    assert upload(api, "a.txt")["accepted"]
    row = api.get("/api/documents").json()["documents"][0]
    assert row["name"] == "a.txt" and row["state"] == "queued" and row["searchable"] is False
