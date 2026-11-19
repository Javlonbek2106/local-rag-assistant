"""Roadmap M4.1a, step 7, against a REAL PostgreSQL in Docker: the three roles, adopting an old single-role database
without losing data, the permissions of the running application, the migration lifecycle (including many processes
starting at once) and password rotation.

The server here is the same image as docker-compose.yml, started the way the old compose file started it (a superuser
`rag` with the password `rag`), so "adopt an existing development database" is the situation under test. Nothing touches
the compose stack the application uses. One test starts the real docker-compose.yml under a different project name.
"""
import json
import logging
import os
import subprocess
import sys
import threading
import uuid
from types import SimpleNamespace

import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege
from psycopg.rows import dict_row

from rag import config, db, dbroles
from rag.api import create_app
from rag.auth import checks, secretfiles
from rag.backup import PgTools
from rag.target import Target
from tests import stack as stack_tools

pytestmark = pytest.mark.integration
OLD_NAME = "kept-from-before.txt"


@pytest.fixture(scope="module")
def stack():
    if not stack_tools.docker_available():
        pytest.skip("Docker is not available")
    running = stack_tools.Stack("hard", with_qdrant=False).start()
    yield running
    running.stop()


@pytest.fixture(scope="module")
def world(stack, tmp_path_factory):
    """An old database (one superuser, data in it) that has just been adopted by `dbroles.setup`."""
    mp = pytest.MonkeyPatch()
    secrets = tmp_path_factory.mktemp("pg-secrets") / "secrets"
    mp.setattr(config, "DB_HARDENED", False)
    mp.setattr(config, "DATABASE_URL", stack.database_url)
    mp.setattr(config, "DB_SCHEMA", None)
    db.close()
    db.migrate()  # the old way: one role does everything
    created = db.create_version(OLD_NAME, 5, "f" * 64)
    db.enqueue(created["id"])
    with psycopg.connect(stack.database_url, row_factory=dict_row) as conn:
        before = {"documents": conn.execute("SELECT count(*) AS n FROM documents").fetchone()["n"],
                  "versions": conn.execute("SELECT count(*) AS n FROM document_versions").fetchone()["n"],
                  "applied": [r["version"] for r in conn.execute("SELECT version FROM schema_migrations")],
                  "owner": conn.execute("SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = 'public'")
                  .fetchall()}
    db.close()
    mp.setattr(config, "DB_HARDENED", True)
    mp.setattr(config, "DB_HOST", "127.0.0.1")
    mp.setattr(config, "DB_PORT", stack.pg_port)
    mp.setattr(config, "DB_NAME", "rag")
    mp.setattr(config, "SECRETS_DIR", secrets)
    mp.setattr(config, "SECURITY_LOG_DIR", secrets.parent / "security-log")
    dbroles.ensure_secrets()
    with pytest.raises(dbroles.DbSetupError) as refused:  # the old open superuser is never taken over by surprise
        dbroles.setup(stack.pg_container)
    assert str(refused.value) == dbroles.LEGACY_WARNING
    steps = dbroles.setup(stack.pg_container, adopt_legacy=True)
    yield SimpleNamespace(stack=stack, secrets=secrets, before=before, steps=steps, created=created)
    db.close()
    mp.undo()


@pytest.fixture
def hardened(world):
    db.close()
    yield world
    db.close()


@pytest.fixture
def own_schema(world, monkeypatch):
    """An empty schema owned by rag_migrator (what a fresh install looks like), used instead of `public`."""
    name = f"life_{uuid.uuid4().hex[:8]}"
    dbroles.psql(f"CREATE SCHEMA {name} AUTHORIZATION rag_migrator;", world.stack.pg_container)
    monkeypatch.setattr(config, "DB_SCHEMA", name)
    db.close()
    yield name
    db.close()
    dbroles.psql(f"DROP SCHEMA {name} CASCADE;", world.stack.pg_container)


def as_role(role, **kwargs):
    return psycopg.connect(dbroles.conninfo(role), password=dbroles.read_password(role), autocommit=True,
                           row_factory=dict_row, **kwargs)


def bootstrap_rows(world, sql):
    out = dbroles.psql(sql, world.stack.pg_container)
    return [line.split("|") for line in out.strip().splitlines()]


# ---------- adopting an old database ----------

def test_the_old_database_keeps_every_row_and_every_applied_migration(hardened):
    with as_role(dbroles.APP) as conn:
        assert conn.execute("SELECT count(*) AS n FROM documents").fetchone()["n"] == hardened.before["documents"]
        versions = conn.execute("SELECT count(*) AS n FROM document_versions").fetchone()["n"]
        assert versions == hardened.before["versions"]
        done = [r["version"] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
    assert set(hardened.before["applied"]) <= set(done) and "0005_principals" in done
    assert db.get_document(OLD_NAME)["name"] == OLD_NAME


def test_what_the_old_superuser_owned_now_belongs_to_the_migrator(hardened):
    assert hardened.before["owner"] == [{"tableowner": "rag"}]
    owners = bootstrap_rows(hardened, "SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = 'public'")
    assert owners == [["rag_migrator"]]
    assert bootstrap_rows(hardened, "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = 'rag'") == [
        ["rag_migrator"]]


def test_the_old_shared_login_is_closed_everywhere(hardened):
    with pytest.raises(psycopg.OperationalError):  # over the network, with the password it always had
        psycopg.connect(f"postgresql://rag:rag@127.0.0.1:{hardened.stack.pg_port}/rag", connect_timeout=5)
    with pytest.raises(dbroles.DbSetupError):  # and from inside the container, where the server trusts the socket
        dbroles.psql("SELECT 1", hardened.stack.pg_container, user="rag")


def test_the_three_roles_have_exactly_the_attributes_they_should(hardened):
    rows = {r[0]: r[1:] for r in bootstrap_rows(hardened, (
        "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolcanlogin FROM pg_roles "
        "WHERE rolname IN ('rag_bootstrap', 'rag_migrator', 'rag_app')"))}
    assert rows["rag_bootstrap"][0] == "t"
    for role in ("rag_migrator", "rag_app"):
        assert rows[role] == ["f", "f", "f", "f", "f", "t"]
    assert bootstrap_rows(hardened, "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
                                    "WHERE r.rolname IN ('rag_app', 'rag_migrator')") == [["0"]]


def test_set_up_reports_every_check_and_only_the_network_rule_is_for_the_compose_file(hardened):
    result = dict(dbroles.verify())
    # this server has the image's default pg_hba.conf; the compose file's one (rejecting rag_bootstrap) is tested below
    assert result.pop("bootstrap_closed_over_network") is False
    assert result and all(result.values()), result


def test_set_up_can_be_repeated_and_repairs_a_password_that_drifted(hardened):
    dbroles.setup(hardened.stack.pg_container)
    with as_role(dbroles.APP) as conn:
        assert conn.execute("SELECT 1 AS one").fetchone()["one"] == 1
    # somebody changed the role's password on the server: running set-up again makes the file and the server agree
    dbroles.psql(f"ALTER ROLE rag_app PASSWORD '{dbroles.scram_verifier('Another-Password-Entirely-9999')}'",
                 hardened.stack.pg_container)
    with pytest.raises(psycopg.OperationalError):
        as_role(dbroles.APP)
    dbroles.setup(hardened.stack.pg_container)
    with as_role(dbroles.APP) as conn:
        assert conn.execute("SELECT 1 AS one").fetchone()["one"] == 1


# ---------- what the running application may and may not do ----------

@pytest.mark.parametrize("statement", [
    "CREATE TABLE intruder (id int)", "CREATE SCHEMA intruder", "CREATE TEMP TABLE intruder (id int)",
    "ALTER TABLE documents ADD COLUMN intruder int", "DROP TABLE documents", "TRUNCATE documents",
    "DROP INDEX documents_name_key", "INSERT INTO schema_migrations (version) VALUES ('9999_intruder')",
    "DELETE FROM schema_migrations", "UPDATE principals SET disabled_at = now()",
    "INSERT INTO principals (kind) VALUES ('other')", "DELETE FROM principals", "CREATE ROLE intruder",
    "ALTER ROLE rag_app SUPERUSER", "CREATE EXTENSION IF NOT EXISTS pgcrypto", "COPY documents TO PROGRAM 'id'",
    "SELECT pg_read_file('/etc/passwd')", "ALTER TABLE documents OWNER TO rag_app",
    "CREATE FUNCTION intruder() RETURNS int LANGUAGE sql AS 'SELECT 1'"])
def test_the_running_application_cannot_do_this(hardened, statement):
    with as_role(dbroles.APP) as conn, pytest.raises(psycopg.Error) as error:
        conn.execute(statement)
    assert isinstance(error.value, InsufficientPrivilege), (statement, error.value)


def test_the_running_application_still_does_its_whole_job_with_those_limits(hardened):
    """The real lifecycle of a document through rag.db, as rag_app: nothing was lost by taking its rights away."""
    first = db.create_version("job-as-app.txt", 3, "a" * 64)
    db.enqueue(first["id"])
    job = db.claim_next("w1")
    while job and job["id"] != first["id"]:
        job = db.claim_next("w1")
    assert job and db.set_stage(job["id"], "w1", "EMBEDDING") and db.set_progress(job["id"], "w1", "chunks", 1, 2)
    assert db.heartbeat(job["id"], "w1") and db.set_stage(job["id"], "w1", "VERIFYING")
    assert db.record_verified(job["id"], "w1", 2) and db.activate(job["id"], "w1", 2) == "activated"
    second = db.create_version("job-as-app.txt", 3, "b" * 64)
    db.enqueue(second["id"])
    db.fail(db.claim_next("w1")["id"], "w1", "boom", retry=False)
    db.list_documents(), db.list_versions("job-as-app.txt"), db.status_snapshot(), db.queue_length(), db.ping()
    db.requeue_stale(config.JOB_STALE_AFTER_S)
    db.delete_document("job-as-app.txt")
    assert db.get_document("job-as-app.txt") is None


def test_the_connector_checks_pass_for_the_application_role_and_tell_the_other_roles_apart(hardened):
    assert checks.db_role_unprivileged() and checks.db_runtime_permissions() and checks.db_password_not_default()
    with as_role(dbroles.MIGRATOR) as conn:  # the migrator may change the schema: the same question says so
        row = conn.execute(checks.RUNTIME_SQL).fetchone()
    assert row["who"] == "rag_migrator" and row["creates_objects"] and row["owns_objects"]


def test_backup_runs_as_the_application_role_and_does_not_show_its_password(hardened, tmp_path):
    target = Target.from_config()
    assert dbroles.read_password(dbroles.APP) not in str(target.describe())
    dump = tmp_path / "db.dump"
    PgTools(container=hardened.stack.pg_container).dump(target, dump)
    assert dump.stat().st_size > 1000


# ---------- the migration lifecycle ----------

def test_when_nothing_is_pending_the_migrator_is_never_used(hardened, monkeypatch):
    def forbidden():
        raise AssertionError("the migrator connected although nothing was pending")

    monkeypatch.setattr(db, "_migrator_connection", forbidden)
    db.migrate()


def test_a_pending_migration_is_applied_by_the_migrator_and_the_new_table_is_closed_to_the_application(
        own_schema, tmp_path, monkeypatch):
    copy = tmp_path / "migrations"
    copy.mkdir()
    for path in db.MIGRATIONS.glob("*.sql"):
        (copy / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    (copy / "0006_probe.sql").write_text("CREATE TABLE probe (id int PRIMARY KEY); INSERT INTO probe VALUES (1);",
                                         encoding="utf-8")
    monkeypatch.setattr(db, "MIGRATIONS", copy)
    db.migrate()
    with as_role(dbroles.MIGRATOR, options=f"-c search_path={own_schema}") as conn:
        assert conn.execute("SELECT pg_get_userbyid(relowner) AS o FROM pg_class WHERE relname = 'probe'"
                            ).fetchone()["o"] == "rag_migrator"
    with db.pool().connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM documents").fetchone()["n"] == 0
        assert "0006_probe" in {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
        with pytest.raises(InsufficientPrivilege):  # decided by nobody yet: closed, not open
            conn.execute("SELECT * FROM probe")


def test_without_the_migrators_secret_a_pending_migration_fails_and_nothing_is_applied(own_schema, monkeypatch):
    path = config.SECRETS_DIR / dbroles.SECRET_FILES[dbroles.MIGRATOR]
    kept = path.read_text(encoding="utf-8")
    path.unlink()
    try:
        with pytest.raises(secretfiles.SecretError):
            db.migrate()
    finally:
        secretfiles.write_secret(dbroles.SECRET_FILES[dbroles.MIGRATOR], kept)
    with as_role(dbroles.APP, options=f"-c search_path={own_schema}") as conn:
        with pytest.raises(psycopg.Error):  # not even schema_migrations exists: the application role did not step in
            conn.execute("SELECT count(*) FROM schema_migrations")


def test_many_threads_starting_together_apply_each_migration_exactly_once(own_schema, caplog):
    caplog.set_level(logging.INFO, logger="rag.db")
    workers, errors, barrier = 8, [], threading.Barrier(8)
    db.pool()  # made before the threads race for it (building it is not what is under test)

    def start():
        barrier.wait()
        try:
            db.migrate()
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=start) for _ in range(workers)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert not errors
    applied = [r.getMessage() for r in caplog.records if r.getMessage().startswith("applied migration")]
    assert len(applied) == len(list(db.MIGRATIONS.glob("*.sql"))) == len(set(applied))
    with db.pool().connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM schema_migrations").fetchone()["n"] == len(applied)
        assert conn.execute("SELECT count(*) AS n FROM principals").fetchone()["n"] == 1


def test_several_processes_starting_together_do_not_clash_either(own_schema, world):
    code = ("import logging; logging.basicConfig(level=logging.INFO, format='%(message)s'); "
            "from rag import db; db.migrate(); db.close()")
    env = {**os.environ, "RAG_DB_HARDENED": "1", "RAG_DB_HOST": "127.0.0.1", "RAG_DB_PORT": str(world.stack.pg_port),
           "RAG_DB_NAME": "rag", "RAG_SECRETS_DIR": str(world.secrets), "RAG_DB_SCHEMA": own_schema,
           "PYTHONDONTWRITEBYTECODE": "1"}
    processes = [subprocess.Popen([sys.executable, "-c", code], cwd=config.ROOT, env=env, stderr=subprocess.PIPE,
                                  stdout=subprocess.PIPE) for _ in range(4)]
    outputs = [p.communicate(timeout=120) for p in processes]
    assert [p.returncode for p in processes] == [0, 0, 0, 0], [o[1][-300:] for o in outputs]
    applied = [line for _, err in outputs for line in err.decode().splitlines() if line.startswith("applied migration")]
    assert len(applied) == len(set(applied)) == len(list(db.MIGRATIONS.glob("*.sql")))


def test_the_api_starts_and_answers_readiness_as_the_application_role(hardened, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(config, "CONNECTOR_ENABLED", True)
    monkeypatch.setattr(config, "SECURITY_LOG_DIR", tmp_path / "security-log")
    monkeypatch.setattr(config, "STORAGE_DIR", tmp_path / "storage")
    with TestClient(create_app(db), base_url=f"http://127.0.0.1:{config.PORT}") as client:
        answer = client.get("/readyz")
        text = json.dumps(answer.json())
        for role in dbroles.ROLES:
            assert dbroles.read_password(role) not in text
        assert answer.status_code == 200 and answer.json()["checks"]["connector"] == "ok", answer.json()


# ---------- credential rotation ----------

def test_rotating_the_application_password_closes_the_old_one_and_new_connections_use_the_new(hardened):
    old = dbroles.read_password(dbroles.APP)
    with db.pool().connection() as conn:  # a connection made before the rotation keeps working
        dbroles.rotate(dbroles.APP, hardened.stack.pg_container)
        assert conn.execute("SELECT 1 AS one").fetchone()["one"] == 1
    new = dbroles.read_password(dbroles.APP)
    assert new != old and dbroles.password_is_strong(new)
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(dbroles.conninfo(dbroles.APP), password=old, connect_timeout=5)
    connection = db._RuntimeConnection.connect(dbroles.conninfo(dbroles.APP))  # what the pool does for a new one
    connection.close()
    db.close()
    assert db.get_document(OLD_NAME)["name"] == OLD_NAME


def test_rotating_the_migrator_and_the_bootstrap_passwords(hardened):
    for role in (dbroles.MIGRATOR, dbroles.BOOTSTRAP):
        old = dbroles.read_password(role)
        dbroles.rotate(role, hardened.stack.pg_container)
        assert dbroles.read_password(role) != old
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(dbroles.conninfo(role), password=old, connect_timeout=5)
    with as_role(dbroles.MIGRATOR) as conn:
        assert conn.execute("SELECT 1 AS one").fetchone()["one"] == 1


def test_a_failed_rotation_leaves_the_working_password_in_place(hardened):
    before = dbroles.read_password(dbroles.APP)
    with pytest.raises(dbroles.DbSetupError):
        dbroles.rotate(dbroles.APP, "no-such-container-" + uuid.uuid4().hex[:6])
    assert dbroles.read_password(dbroles.APP) == before and not list(config.SECRETS_DIR.glob("*.new"))
    with as_role(dbroles.APP) as conn:
        assert conn.execute("SELECT 1 AS one").fetchone()["one"] == 1


def test_no_password_or_verifier_was_logged_by_anything_above(hardened, caplog):
    caplog.set_level(logging.DEBUG)
    dbroles.setup(hardened.stack.pg_container)
    dbroles.verify()
    text = caplog.text
    assert all(dbroles.read_password(role) not in text for role in dbroles.ROLES) and "SCRAM-SHA-256$" not in text


# ---------- the real docker-compose.yml ----------

def test_the_compose_file_starts_a_server_that_the_bootstrap_role_cannot_enter_over_the_network(tmp_path, monkeypatch):
    if not stack_tools.docker_available():
        pytest.skip("Docker is not available")
    project, port = f"ragtest-{uuid.uuid4().hex[:8]}", stack_tools.free_port()
    secrets = tmp_path / "secrets"
    monkeypatch.setattr(config, "SECRETS_DIR", secrets)
    monkeypatch.setattr(config, "SECURITY_LOG_DIR", tmp_path / "security-log")
    for name, value in (("DB_HARDENED", True), ("DB_HOST", "127.0.0.1"), ("DB_PORT", port), ("DB_NAME", "rag"),
                        ("DB_SCHEMA", None)):
        monkeypatch.setattr(config, name, value)
    dbroles.ensure_secrets()
    env = {**os.environ, "RAG_SECRETS_DIR": str(secrets), "RAG_PG_PORT": str(port)}
    compose = ["docker", "compose", "-p", project, "-f", str(config.ROOT / "docker-compose.yml")]
    container = f"{project}-postgres-1"
    try:
        up = subprocess.run([*compose, "up", "-d", "--wait", "postgres"], env=env, capture_output=True, text=True,
                            timeout=240)
        assert up.returncode == 0, up.stderr[-500:]
        inspected = subprocess.run(["docker", "inspect", container], capture_output=True, text=True).stdout
        assert all(dbroles.read_password(role) not in inspected for role in dbroles.ROLES)
        assert "POSTGRES_PASSWORD_FILE" in inspected and '"POSTGRES_PASSWORD=' not in inspected
        steps = dbroles.setup(container)
        assert [s[0] for s in steps] == ["roles", "migrations"]  # a new volume has no old role to retire
        results = dict(dbroles.verify())
        assert results and all(results.values()), results  # now including: bootstrap closed, old `rag` closed
        with as_role(dbroles.APP) as conn:
            assert conn.execute("SELECT count(*) AS n FROM principals").fetchone()["n"] == 1
    finally:
        db.close()
        subprocess.run([*compose, "down", "-v"], env=env, capture_output=True, timeout=120)
