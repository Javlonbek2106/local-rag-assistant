"""Roadmap M4.1a, decision D-7: what the PostgreSQL role the application connects as may and may not do, checked
against a real server with real roles.

Roles belong to the whole PostgreSQL server, not to one schema: every role made here has a unique name and is always
dropped again (with what it owns and was granted), whatever the test did.
"""
import secrets
import threading

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from rag import config, db
from rag.auth import checks
from rag.auth.principals import PrincipalError, Principals

pytestmark = pytest.mark.db


@pytest.fixture
def make_role(database):
    """make_role(...) -> (name, password) of a new LOGIN role; `make_role.connect_as` points the application at it.
    Everything is undone at the end."""
    made, original = [], config.DATABASE_URL
    admin = psycopg.connect(original, autocommit=True)

    def create(attributes="NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS", member_of=(), grants=True):
        name, password = f"rag_test_role_{secrets.token_hex(4)}", secrets.token_urlsafe(24)
        admin.execute(f"CREATE ROLE {name} LOGIN PASSWORD '{password}' {attributes}")
        made.append(name)
        for parent in member_of:
            admin.execute(f"GRANT {parent} TO {name}")
        if grants:  # the minimum the application needs to read the principals: connect, see the schema, read the table
            admin.execute(f"GRANT USAGE ON SCHEMA {config.DB_SCHEMA} TO {name}")
            admin.execute(f"GRANT SELECT ON {config.DB_SCHEMA}.principals TO {name}")
        return name, password

    def connect_as(name, password):
        db.close()  # the pool of the previous role goes away with its connections
        config.DATABASE_URL = make_conninfo(original, user=name, password=password)

    create.connect_as = connect_as
    yield create
    db.close()
    config.DATABASE_URL = original
    for name in made:
        admin.execute(f"DROP OWNED BY {name}")
        admin.execute(f"DROP ROLE {name}")
    admin.close()


def test_the_superuser_the_tests_connect_as_fails_the_check(database):
    assert checks.db_role_unprivileged() is False


def test_an_ordinary_role_passes_and_can_still_read_the_principals(make_role):
    name, password = make_role()
    make_role.connect_as(name, password)
    assert checks.db_role_unprivileged() is True
    assert Principals().local_owner()["kind"] == "local-owner"


@pytest.mark.parametrize("attribute", ["CREATEROLE", "CREATEDB", "REPLICATION", "BYPASSRLS", "SUPERUSER"])
def test_any_one_dangerous_attribute_fails_the_check(make_role, attribute):
    attributes = {"SUPERUSER": "SUPERUSER", "CREATEROLE": "CREATEROLE", "CREATEDB": "CREATEDB",
                  "REPLICATION": "REPLICATION", "BYPASSRLS": "BYPASSRLS"}[attribute]
    name, password = make_role(attributes)
    make_role.connect_as(name, password)
    assert checks.db_role_unprivileged() is False


@pytest.mark.parametrize("parent", ["pg_execute_server_program", "pg_read_server_files", "pg_write_server_files",
                                    "pg_read_all_data", "pg_write_all_data"])
def test_membership_of_a_role_that_reads_files_runs_programs_or_reads_everything_fails_the_check(make_role, parent):
    name, password = make_role(member_of=[parent])
    make_role.connect_as(name, password)
    assert checks.db_role_unprivileged() is False


def test_a_role_that_may_read_the_principals_cannot_change_them_or_the_schema(make_role):
    """The boundary the provisioning is meant to give: reading, no writing, no DDL."""
    name, password = make_role()
    make_role.connect_as(name, password)
    with db.pool().connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM principals").fetchone()["n"] == 1
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE principals SET display_label = 'changed'")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM principals")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("CREATE TABLE tenant_leak (x int)")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM documents")  # not granted: nothing beyond what was given


def test_a_role_with_no_rights_on_the_schema_is_a_failure_that_names_no_cause(make_role):
    name, password = make_role(grants=False)
    make_role.connect_as(name, password)
    assert checks.db_role_unprivileged() is True  # the role itself is harmless
    with pytest.raises(PrincipalError) as error:
        Principals().local_owner()
    assert error.value.code == "unreachable" and name not in str(error.value) and password not in str(error.value)


def test_a_wrong_password_is_a_failed_check_within_seconds_not_a_half_minute_hang(make_role, monkeypatch):
    import time

    monkeypatch.setattr(config, "SECURITY_DB_TIMEOUT_S", 2)
    name, _password = make_role()
    make_role.connect_as(name, "not-the-password")
    started = time.monotonic()
    assert _safe(checks.db_role_unprivileged) is False
    assert time.monotonic() - started < 10  # the pool alone would have waited 30 s


def _safe(check):
    try:
        return bool(check())
    except Exception:  # noqa: BLE001 - this is what SecurityChecks does with a check that cannot run
        return False


def test_the_role_check_is_safe_from_many_threads_at_once(make_role):
    name, password = make_role()
    make_role.connect_as(name, password)
    db.pool()  # open, as the start-up of the application does before any request: a cold `pool()` called by
    results = []  # several threads at once may make several pools (an existing property of db.pool, not tested here)
    threads = [threading.Thread(target=lambda: results.append(checks.db_role_unprivileged())) for _ in range(4)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert results == [True] * 4
