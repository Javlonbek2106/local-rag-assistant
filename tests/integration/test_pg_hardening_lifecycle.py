"""Acceptance audit of M4.1a against REAL servers in Docker: a migration that fails, the upgrade of a volume made by the
OLD compose file (through the real docker-compose.yml), and backup and restore of a three-role system.

Needs RAG_INTEGRATION=1 and Docker. Nothing here touches the running compose stack.
"""
import json
import os
import subprocess
import time
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from rag import backup, config, db, dbroles, reconcile, store, versioning
from rag.api import create_app
from rag.auth import checks
from rag.backup import PgTools
from rag.target import Target
from tests import stack as stack_tools
from tests.integration.test_backup_restore import SOURCE_COLLECTION, Source

pytestmark = pytest.mark.integration


def need_docker():
    if not stack_tools.docker_available():
        pytest.skip("Docker is not available")


def aim_at(monkeypatch, port, secrets, log_dir):
    for name, value in (("DB_HARDENED", True), ("DB_HOST", "127.0.0.1"), ("DB_PORT", port), ("DB_NAME", "rag"),
                        ("DB_SCHEMA", None), ("SECRETS_DIR", secrets), ("SECURITY_LOG_DIR", log_dir)):
        monkeypatch.setattr(config, name, value)
    db.close()


def events_of(log_dir):
    return [json.loads(line) for line in (log_dir / "events.log").read_text(encoding="utf-8").splitlines()]


# ---------- a migration that fails ----------

@pytest.fixture(scope="module")
def pg():
    need_docker()
    running = stack_tools.Stack("life", with_qdrant=False).start()
    yield running
    running.stop()


@pytest.fixture
def three_roles(pg, tmp_path, monkeypatch):
    aim_at(monkeypatch, pg.pg_port, tmp_path / "secrets", tmp_path / "security-log")
    dbroles.ensure_secrets()
    dbroles.setup(pg.pg_container, adopt_legacy=True)  # on a server set up before: the new files are put in force
    yield pg
    db.close()


def test_a_migration_that_fails_changes_nothing_and_does_not_leave_the_lock_behind(pg, tmp_path, monkeypatch):
    aim_at(monkeypatch, pg.pg_port, tmp_path / "secrets", tmp_path / "security-log")
    dbroles.ensure_secrets()
    dbroles.setup(pg.pg_container, adopt_legacy=True, migrate=False)
    schema = f"fail_{uuid.uuid4().hex[:8]}"
    dbroles.psql(f"CREATE SCHEMA {schema} AUTHORIZATION rag_migrator;", pg.pg_container)
    monkeypatch.setattr(config, "DB_SCHEMA", schema)
    copy = tmp_path / "migrations"
    copy.mkdir()
    for path in db.MIGRATIONS.glob("*.sql"):
        (copy / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    (copy / "0006_broken.sql").write_text("CREATE TABLE half_done (id int); SELECT 1 / 0;", encoding="utf-8")
    monkeypatch.setattr(db, "MIGRATIONS", copy)
    db.close()
    with pytest.raises(psycopg.errors.DivisionByZero):
        db.migrate()
    with psycopg.connect(dbroles.conninfo(dbroles.MIGRATOR), password=dbroles.read_password(dbroles.MIGRATOR),
                         autocommit=True, options=f"-c search_path={schema}") as conn:
        applied = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
        # every earlier migration stays applied; the broken one left neither a record nor a half-made table
        assert applied == sorted(p.stem for p in db.MIGRATIONS.glob("*.sql") if p.stem < "0006")
        assert conn.execute("SELECT to_regclass(%s)", (f"{schema}.half_done",)).fetchone()[0] is None
        # the advisory lock went with the failed run: nobody is kept waiting
        assert conn.execute("SELECT pg_try_advisory_lock(%s)", (db.MIGRATION_LOCK,)).fetchone()[0] is True
        conn.execute("SELECT pg_advisory_unlock(%s)", (db.MIGRATION_LOCK,))
    (copy / "0006_broken.sql").write_text("CREATE TABLE fixed (id int);", encoding="utf-8")  # fixed: the next run works
    db.migrate()
    db.close()
    dbroles.psql(f"DROP SCHEMA {schema} CASCADE;", pg.pg_container)


def test_the_lock_is_a_session_lock_so_a_migrator_that_dies_releases_it(three_roles):
    holder = psycopg.connect(dbroles.conninfo(dbroles.MIGRATOR), password=dbroles.read_password(dbroles.MIGRATOR),
                             autocommit=True)
    holder.execute("SELECT pg_advisory_lock(%s)", (db.MIGRATION_LOCK,))
    waiter = psycopg.connect(dbroles.conninfo(dbroles.MIGRATOR), password=dbroles.read_password(dbroles.MIGRATOR),
                             autocommit=True)
    assert waiter.execute("SELECT pg_try_advisory_lock(%s)", (db.MIGRATION_LOCK,)).fetchone()[0] is False
    holder.close()  # the process died, the connection went with it
    assert waiter.execute("SELECT pg_try_advisory_lock(%s)", (db.MIGRATION_LOCK,)).fetchone()[0] is True
    waiter.close()


# ---------- the real API as rag_app ----------

def test_the_real_api_uploads_lists_versions_and_deletes_as_the_application_role(three_roles, tmp_path, monkeypatch):
    from tests.conftest import FakeQdrant
    fake = FakeQdrant()
    monkeypatch.setattr(store, "get_client", lambda: fake)
    monkeypatch.setattr(store, "ensure_collection", lambda client, size: None)
    monkeypatch.setattr(config, "STORAGE_DIR", tmp_path / "storage")
    monkeypatch.setattr(config, "WARMUP", False)
    with TestClient(create_app(db), base_url=f"http://127.0.0.1:{config.PORT}") as client:
        sent = client.post("/api/documents", files=[("files", ("audit.txt", b"hello audit", "text/plain"))])
        assert sent.status_code == 200 and sent.json()["results"][0]["accepted"] is True, sent.text
        names = [d["name"] for d in client.get("/api/documents").json()["documents"]]
        assert "audit.txt" in names
        assert client.get("/api/documents/audit.txt/versions").status_code == 200
        assert client.get("/healthz").status_code == 200
        # the same file again is recognised as unchanged (a read and a write through the application's role)
        again = client.post("/api/documents", files=[("files", ("audit.txt", b"hello audit", "text/plain"))])
        assert again.json()["results"][0]["unchanged"] is True
        deleted = client.delete("/api/documents/audit.txt")
        assert deleted.status_code == 200, deleted.text
        assert "audit.txt" not in [d["name"] for d in client.get("/api/documents").json()["documents"]]


# ---------- upgrading a volume made by the OLD compose file ----------

def test_a_volume_of_the_old_compose_file_is_upgraded_only_with_the_operators_word_and_keeps_its_data(
        tmp_path, monkeypatch):
    need_docker()
    project, port, old_port = f"ragtest-{uuid.uuid4().hex[:8]}", stack_tools.free_port(), stack_tools.free_port()
    volume, old_container = f"{project}_pg_data", f"{project}-old"
    secrets, log_dir = tmp_path / "secrets", tmp_path / "security-log"
    env = {**os.environ, "RAG_SECRETS_DIR": str(secrets), "RAG_PG_PORT": str(port)}
    compose = ["docker", "compose", "-p", project, "-f", str(config.ROOT / "docker-compose.yml")]
    container = f"{project}-postgres-1"
    subprocess.run(["docker", "volume", "create", volume], check=True, capture_output=True)
    try:
        # 1. the old server: superuser rag/rag, data in the volume (what the user's machine looks like today)
        subprocess.run(["docker", "run", "-d", "--rm", "--name", old_container, "-e", "POSTGRES_USER=rag", "-e",
                        "POSTGRES_PASSWORD=rag", "-e", "POSTGRES_DB=rag", "-v", f"{volume}:/var/lib/postgresql/data",
                        "-p", f"127.0.0.1:{old_port}:5432", "postgres:17-alpine"], check=True, capture_output=True)
        deadline = time.time() + 60
        while time.time() < deadline:
            probe = subprocess.run(["docker", "exec", old_container, "psql", "-U", "rag", "-d", "rag", "-tAc",
                                    "SELECT 1"], capture_output=True, text=True)
            if probe.stdout.strip() == "1":
                break
            time.sleep(1)
        monkeypatch.setattr(config, "DB_HARDENED", False)
        monkeypatch.setattr(config, "DB_SCHEMA", None)
        monkeypatch.setattr(config, "DATABASE_URL", f"postgresql://rag:rag@127.0.0.1:{old_port}/rag")
        db.close()
        db.migrate()
        db.create_version("old-data.txt", 3, "c" * 64)
        db.close()
        subprocess.run(["docker", "stop", old_container], check=True, capture_output=True)

        # 2. the new compose file on the SAME volume
        aim_at(monkeypatch, port, secrets, log_dir)
        dbroles.ensure_secrets()
        up = subprocess.run([*compose, "up", "-d", "--wait", "postgres"], env=env, capture_output=True, text=True,
                            timeout=240)
        assert up.returncode == 0, up.stderr[-500:]

        # the new access rules close the old login over the network at once, before any set-up
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(f"postgresql://rag:rag@127.0.0.1:{port}/rag", connect_timeout=5)

        # 3. set-up refuses to go on until the operator has read the warning; it changes nothing meanwhile
        with pytest.raises(dbroles.DbSetupError) as refused:
            dbroles.setup(container)
        assert str(refused.value) == dbroles.LEGACY_WARNING
        assert dbroles.psql("SELECT rolcanlogin FROM pg_roles WHERE rolname = 'rag'", container, "rag").strip() == "t"
        others = dbroles.psql("SELECT count(*) FROM pg_roles WHERE rolname LIKE 'rag\\_%'", container, "rag")
        assert others.strip() == "0"  # no role was made

        # 4. with the operator's word: adopted, the data is where it was, everything checks out
        steps = dbroles.setup(container, adopt_legacy=True)
        assert [s[0] for s in steps] == ["roles", "legacy", "migrations"]
        results = dict(dbroles.verify())
        assert results and all(results.values()), results
        assert db.get_document("old-data.txt")["name"] == "old-data.txt"
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(f"postgresql://rag:rag@127.0.0.1:{port}/rag", connect_timeout=5)

        # 5. repeating it is harmless and needs no further consent
        assert [s[0] for s in dbroles.setup(container)] == ["roles", "legacy", "migrations"]
        assert db.get_document("old-data.txt") is not None
        recorded = [(e["event"], e["reason"]) for e in events_of(log_dir)]
        assert ("legacy_role_closed", "ok") in recorded and recorded.count(("db_setup", "ok")) == 2
        assert "rag:rag" not in (log_dir / "events.log").read_text(encoding="utf-8")
    finally:
        db.close()
        subprocess.run([*compose, "down", "-v"], env=env, capture_output=True, timeout=120)
        subprocess.run(["docker", "rm", "-f", old_container], capture_output=True)
        subprocess.run(["docker", "volume", "rm", "-f", volume], capture_output=True)


# ---------- backup and restore of a three-role system ----------

def test_a_three_role_system_is_backed_up_as_rag_app_and_restored_into_a_new_three_role_system(tmp_path, monkeypatch):
    need_docker()
    source_stack, target_stack = stack_tools.Stack("hsrc"), stack_tools.Stack("hdst")
    try:
        source_stack.start()
        target_stack.start()
        # ---- the source: three roles, data written by the application role
        source_secrets = tmp_path / "source-secrets"
        aim_at(monkeypatch, source_stack.pg_port, source_secrets, tmp_path / "log-source")
        dbroles.ensure_secrets()
        dbroles.setup(source_stack.pg_container, adopt_legacy=True)
        storage = tmp_path / "source-storage"
        monkeypatch.setattr(config, "STORAGE_DIR", storage)
        source = Source(source_stack, storage)
        source.target = Target(db.runtime_url(), source_stack.qdrant_url, SOURCE_COLLECTION, storage)
        source.populate()
        folder = backup.backup(source.target, tmp_path / "backups", PgTools(container=source_stack.pg_container))
        manifest = backup.read_manifest(folder)
        text = (folder / "manifest.json").read_text(encoding="utf-8")
        assert manifest["status"] == "complete" and backup.verify(folder) == []
        assert all(dbroles.read_password(role) not in text for role in dbroles.ROLES)
        db.close()

        # ---- the target: an empty server; its OWN roles and passwords (a backup carries neither)
        target_secrets = tmp_path / "target-secrets"
        aim_at(monkeypatch, target_stack.pg_port, target_secrets, tmp_path / "log-target")
        dbroles.ensure_secrets()
        assert dbroles.read_password(dbroles.APP) != (source_secrets / dbroles.SECRET_FILES[dbroles.APP]).read_text(
            encoding="utf-8").strip()
        dbroles.setup(target_stack.pg_container, adopt_legacy=True, migrate=False)  # roles first, no tables yet
        target = Target(dbroles.url_with_password(dbroles.MIGRATOR), target_stack.qdrant_url, "restored_docs",
                        tmp_path / "restored-storage")
        result = backup.restore(folder, target, PgTools(container=target_stack.pg_container))
        assert result["ok"] is True

        # ---- the restored system is brought under the runtime permissions by the same set-up
        dbroles.setup(target_stack.pg_container)
        checked = dict(dbroles.verify())
        assert checked.pop("bootstrap_closed_over_network") is False  # this server has the image's default pg_hba.conf
        assert checked and all(checked.values()), checked
        assert checks.db_role_unprivileged() and checks.db_runtime_permissions() and checks.db_password_not_default()
        owners = dbroles.psql("SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = 'public'",
                              target_stack.pg_container, "rag_bootstrap").split()
        assert owners == ["rag_migrator"]

        # ---- and it works as rag_app: history, generation, rollback, reconcile, the real API
        monkeypatch.setattr(config, "COLLECTION", "restored_docs")
        monkeypatch.setattr(config, "STORAGE_DIR", tmp_path / "restored-storage")
        document = db.get_document("alpha.txt")
        assert document["version"] == 2 and document["activation_generation"] == 2
        runtime = Target(db.runtime_url(), target_stack.qdrant_url, "restored_docs", tmp_path / "restored-storage")
        assert reconcile.run(runtime).exit_code == 0
        assert versioning.rollback(db, runtime.qdrant(), "alpha.txt", 1)["active"]
        assert db.get_document("alpha.txt")["activation_generation"] == 3
        monkeypatch.setattr(store, "get_client", lambda: runtime.qdrant())
        monkeypatch.setattr(config, "QDRANT_URL", target_stack.qdrant_url)
        with TestClient(create_app(db), base_url=f"http://127.0.0.1:{config.PORT}") as client:
            names = [d["name"] for d in client.get("/api/documents").json()["documents"]]
            assert {"alpha.txt", "beta.txt"} <= set(names)
    finally:
        db.close()
        source_stack.stop()
        target_stack.stop()
