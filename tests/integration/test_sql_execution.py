"""M4.4 against REAL external servers in Docker (PostgreSQL 15 and 17, SQL Server 2022): the real validator, the real
service, the real drivers and the real internal database (a throw-away schema).
 What is proved against real servers: ordinary queries return the real rows; an excluded column never comes back, not
even through `*`; rows and bytes are limited; a query that runs too long is stopped and its session is gone; a client
that goes away stops the query; writes are refused by the DATABASE (not only by the validator); an account that can
write is refused and nothing is changed; the connections of every run are closed. Needs RAG_INTEGRATION=1,
RAG_TEST_DATABASE_URL and Docker. The tables a test needs it makes itself and drops afterwards."""

import threading
import time

import pytest

pytest.importorskip("cryptography")
pytest.importorskip("pytds")
pytest.importorskip("sqlglot")

from rag import config  # noqa: E402
from rag.connector.drivers import CancelToken, RunLimits, Target  # noqa: E402
from rag.connector.drivers import driver as load_driver  # noqa: E402
from rag.connector.errors import ConnectorError, DriverError  # noqa: E402
from rag.connector.service import Service  # noqa: E402
from tests.auth_support import Events  # noqa: E402
from tests.connector_support import KeyRing  # noqa: E402
from tests.db.test_connector_service import admin  # noqa: E402
from tests.integration import external_servers as ext  # noqa: E402
from tests.integration.test_connector_databases import (  # noqa: E402, F401
    docker_ready,
    mssql_payload,
    pg_payload,
)
from tests.integration.test_connector_snapshots_real import (  # noqa: E402, F401
    Backend,
    backend,
    mssql_server,
    pg15,
    pg17,
)

# ruff: noqa: F811

pytestmark = [pytest.mark.integration, pytest.mark.db]


@pytest.fixture(autouse=True)
def clean(database):
    with admin() as conn:
        conn.execute("DELETE FROM connections")
    yield
    with admin() as conn:
        conn.execute("DELETE FROM connections")


@pytest.fixture
def service():
    return Service(KeyRing().vault, Events())  # the real drivers, the real resolver, the real policy


@pytest.fixture
def owner():
    from rag.auth.principals import Principals

    return Principals().local_owner()["id"]


BIG = {
    "pg": (
        "CREATE TABLE sales.zz_big AS SELECT g AS n, repeat('x', 1000) AS t FROM generate_series(1, 3000) g; "
        "GRANT SELECT ON sales.zz_big TO reader;",
        "DROP TABLE IF EXISTS sales.zz_big;",
    ),
    "mssql": (
        "WITH n AS (SELECT TOP 3000 ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS n FROM sys.all_objects a "
        "CROSS JOIN sys.all_objects b) SELECT n, REPLICATE(CAST('x' AS varchar(max)), 1000) AS t INTO sales.zz_big "
        "FROM n;",
        "DROP TABLE IF EXISTS sales.zz_big;",
    ),
}


def kind(backend):
    return "mssql" if backend.payload().get("provider") == "mssql" else "pg"


def pick(service, owner, backend, selection, **changes):
    made = service.create(owner, backend.payload(**changes))
    service.discover(owner, made["id"])
    service.put_selections(owner, made["id"], selection)
    return made["id"]


def table(schema, name):
    return {"kind": "table", "schema": schema, "object": name}


def sessions(backend):
    """How many sessions of the connector are still open on the external server."""
    if kind(backend) == "mssql":
        return int(
            backend.server.scalar("SELECT count(*) FROM sys.dm_exec_sessions WHERE program_name = 'rag-connector'")
        )
    return int(backend.server.scalar("SELECT count(*) FROM pg_stat_activity WHERE application_name = 'rag-connector'"))


def target_of(backend, user="reader", password=ext.READER_PASSWORD):
    payload = backend.payload()
    return (
        Target("127.0.0.1", ("127.0.0.1",), payload["port"], payload["database_name"], user, "disable"),
        password,
        load_driver(payload["provider"]),
    )


def limits(**changes):
    values = {
        "timeout_s": 20.0,
        "connect_timeout_s": 10.0,
        "max_rows": 1000,
        "max_bytes": 1048576,
        "max_cell_chars": 2000,
    }
    return RunLimits(**{**values, **changes})


def wait_until(condition, seconds=10):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.1)
    return condition()


# ---------- ordinary queries return the real rows ----------


def test_an_ordinary_query_runs_on_the_real_server_and_returns_its_rows(backend, service, owner):
    cid = pick(service, owner, backend, [table("sales", "customer"), table("sales", "order_line")])
    result = service.execute_sql(owner, cid, "SELECT c.id, c.name, c.balance FROM sales.customer c ORDER BY c.id")
    assert result["status"] == "executed" and result["row_count"] == 1 and result["truncated"] is False
    assert [c["name"] for c in result["columns"]] == ["id", "name", "balance"]
    assert result["rows"][0][:2] == [1, "Hidden Customer Name"] and float(result["rows"][0][2]) == 10.5
    assert sessions(backend) == 0  # the connection was closed


QUERIES = [
    "SELECT count(*) AS n FROM sales.customer",
    "SELECT c.id FROM sales.customer c WHERE c.id IN (SELECT customer_id FROM sales.order_line)",
    "SELECT c.name, count(o.order_id) AS orders FROM sales.customer c LEFT JOIN sales.order_line o ON o.customer_id = "
    "c.id GROUP BY c.name",
    "WITH t AS (SELECT id, name FROM sales.customer) SELECT t.name FROM t WHERE t.id = 1",
    "SELECT x.n FROM (SELECT count(*) AS n FROM sales.order_line) x",
    "SELECT c.id FROM sales.customer c WHERE EXISTS (SELECT 1 FROM sales.order_line o WHERE o.customer_id = c.id)",
    "SELECT c.id, CASE WHEN c.id > 0 THEN 'pos' ELSE 'neg' END AS sign, coalesce(c.name, 'none') AS nm FROM "
    "sales.customer c",
    "SELECT UPPER(c.name) AS u FROM sales.customer c",
    "SELECT c.id FROM sales.customer c UNION SELECT order_id FROM sales.order_line",
    "SELECT c.id, ROW_NUMBER() OVER (ORDER BY c.id) AS rn FROM sales.customer c",
    "SELECT 1 AS one",
]


def test_the_canonical_text_the_validator_writes_is_accepted_by_the_real_server_for_a_range_of_queries(
    backend, service, owner
):
    cid = pick(service, owner, backend, [table("sales", "customer"), table("sales", "order_line")])
    for sql in QUERIES:
        result = service.execute_sql(owner, cid, sql)
        assert result["status"] == "executed", sql
        raw = result["row_count"]
        assert raw >= 0 and "--" not in result["executed_sql"]
    assert service.execute_sql(owner, cid, "SELECT count(*) AS n FROM sales.customer")["rows"] == [[1]]


def test_date_and_string_functions_of_each_dialect_run(backend, service, owner):
    cid = pick(service, owner, backend, [table("sales", "customer")])
    if kind(backend) == "mssql":
        sql = (
            "SELECT TOP 1 DATEADD(day, -30, GETDATE()) AS d, ISNULL(c.name, 'x') AS n, LEN(c.name) AS l "
            "FROM sales.customer c"
        )
    else:
        sql = (
            "SELECT now() - interval '30 days' AS d, coalesce(c.name, 'x') AS n, length(c.name) AS l "
            "FROM sales.customer c"
        )
    result = service.execute_sql(owner, cid, sql)
    assert result["row_count"] == 1 and result["rows"][0][2] == len("Hidden Customer Name")


def test_an_excluded_column_never_comes_back_not_even_through_a_star(backend, service, owner):
    hidden = {"kind": "column", "schema": "sales", "object": "customer", "column": "secret_note", "mode": "exclude"}
    cid = pick(service, owner, backend, [table("sales", "customer"), hidden])
    refused = pytest.raises(
        ConnectorError, service.execute_sql, owner, cid, "SELECT secret_note FROM sales.customer"
    ).value
    assert refused.code == "sql_rejected" and refused.reason == "unknown_column"
    for sql in ("SELECT * FROM sales.customer", "SELECT c.* FROM sales.customer c"):
        result = service.execute_sql(owner, cid, sql)
        assert "secret_note" not in [c["name"] for c in result["columns"]]
        assert ext.SECRET_ROW not in str(result) and "secret_note" not in result["executed_sql"]


# ---------- limits, enforced by the server and the driver ----------


def test_rows_are_limited_and_a_database_side_limit_is_in_the_text_that_runs(backend, service, owner):
    with_big = BIG[kind(backend)]
    backend.run(with_big[0])
    try:
        cid = pick(service, owner, backend, [table("sales", "zz_big")])
        cut = service.execute_sql(owner, cid, "SELECT n FROM sales.zz_big ORDER BY n", max_rows=100)
        assert cut["row_count"] == 100 and cut["truncated"] and cut["truncated_by"] == "rows"
        assert ("LIMIT 101" in cut["executed_sql"]) or (
            "TOP 101" in cut["executed_sql"]
        )  # the server stops at 101 rows
        assert [r[0] for r in cut["rows"]] == list(range(1, 101))
        whole = service.execute_sql(owner, cid, "SELECT n FROM sales.zz_big WHERE n <= 10 ORDER BY n")
        assert whole["row_count"] == 10 and whole["truncated"] is False
        union = service.execute_sql(
            owner, cid, "SELECT n FROM sales.zz_big UNION ALL SELECT n FROM sales.zz_big", max_rows=50
        )
        assert union["row_count"] == 50 and union["truncated_by"] == "rows"  # no LIMIT in a union: the driver cuts
        assert sessions(backend) == 0
    finally:
        backend.run(with_big[1])


def test_the_size_of_the_result_and_of_a_value_is_limited(backend, service, owner, monkeypatch):
    with_big = BIG[kind(backend)]
    backend.run(with_big[0])
    try:
        cid = pick(service, owner, backend, [table("sales", "zz_big")])
        monkeypatch.setattr(config, "SQL_EXEC_MAX_BYTES", 20000)
        monkeypatch.setattr(config, "SQL_EXEC_MAX_CELL_CHARS", 100)
        result = service.execute_sql(owner, cid, "SELECT n, t FROM sales.zz_big ORDER BY n")
        assert result["truncated_by"] == "bytes" and 0 < result["row_count"] < 1000
        assert all(len(row[1]) <= 101 for row in result["rows"]) and result["rows"][0][1].endswith("…")
    finally:
        backend.run(with_big[1])


# ---------- time, cancellation, cleanup ----------


def test_a_query_that_runs_too_long_is_stopped_and_its_session_is_gone(backend):
    target, password, driver = target_of(backend)
    sleeper = "SELECT pg_sleep(30)" if kind(backend) == "pg" else "WAITFOR DELAY '00:00:30'; SELECT 1"
    started = time.monotonic()
    with pytest.raises(DriverError) as error:
        driver.run(target, password, sleeper, limits(timeout_s=2.0))
    assert error.value.code == "timeout" and time.monotonic() - started < 10
    assert wait_until(lambda: sessions(backend) == 0)  # nothing is left running on the server


def test_a_client_that_goes_away_stops_the_query_on_the_server(backend):
    target, password, driver = target_of(backend)
    token, outcome = CancelToken(), []
    sleeper = "SELECT pg_sleep(60)" if kind(backend) == "pg" else "WAITFOR DELAY '00:01:00'; SELECT 1"

    def work():
        try:
            driver.run(target, password, sleeper, limits(timeout_s=120.0), token)
        except DriverError as error:
            outcome.append(error.code)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    assert wait_until(lambda: sessions(backend) == 1)  # it is running on the server
    started = time.monotonic()
    token.cancel()
    thread.join(15)
    assert outcome == ["cancelled"] and time.monotonic() - started < 10
    assert wait_until(lambda: sessions(backend) == 0)


# ---------- the database itself refuses writes ----------

WRITES = {
    "pg": [
        "INSERT INTO sales.customer VALUES (9, 'x', 'y', 1)",
        "UPDATE sales.customer SET name = 'x'",
        "DELETE FROM sales.customer",
        "TRUNCATE sales.customer",
        "DROP TABLE sales.customer",
        "CREATE TABLE sales.intruder (a int)",
        "CREATE ROLE intruder",
        "CREATE SCHEMA intruder",
    ],
    "mssql": [
        "INSERT INTO sales.customer (id, name) VALUES (9, N'x')",
        "UPDATE sales.customer SET name = N'x'",
        "DELETE FROM sales.customer",
        "TRUNCATE TABLE sales.customer",
        "DROP TABLE sales.customer",
        "CREATE TABLE sales.intruder (a int)",
        "CREATE SCHEMA intruder",
        "EXEC sp_who",
    ],
}


def test_the_database_refuses_every_write_even_if_the_validator_were_bypassed(backend):
    """The driver runs whatever text it is given; what protects the data is the account and the session. Every write is
    tried against the real server with a read-only account, directly (as if there were no validator): none works."""
    before = backend.server.checksum()
    target, password, driver = target_of(backend)
    for sql in WRITES[kind(backend)]:
        with pytest.raises(DriverError) as error:
            driver.run(target, password, sql, limits())
        assert error.value.code in ("query_error", "permission_denied", "failed"), (sql, error.value.code)
    assert backend.server.checksum() == before and sessions(backend) == 0


def test_an_account_that_can_write_is_refused_and_nothing_is_run_or_changed(backend, service, owner):
    before = backend.server.checksum()
    payload = {"username": "writer", "password": ext.WRITER_PASSWORD, "name": "writer"}
    cid = pick(service, owner, backend, [table("sales", "customer")], **payload)
    error = pytest.raises(ConnectorError, service.execute_sql, owner, cid, "SELECT id FROM sales.customer").value
    assert (error.code, error.status) == ("account_can_write", 409)
    assert backend.server.checksum() == before and sessions(backend) == 0


def test_an_administrator_account_is_refused_too(backend, service, owner):
    if kind(backend) == "mssql":
        pytest.skip("the sa account is not enabled for the throw-away SQL Server used here")
    cid = pick(
        service,
        owner,
        backend,
        [table("sales", "customer")],
        username="postgres",
        password=ext.ADMIN_PASSWORD,
        name="admin",
    )
    assert pytest.raises(
        ConnectorError, service.execute_sql, owner, cid, "SELECT id FROM sales.customer"
    ).value.code == ("account_can_write")


def test_a_table_the_account_may_not_read_is_a_database_error_not_a_crash(backend, service, owner):
    cid = pick(service, owner, backend, [table("sales", "customer")])
    if kind(backend) == "mssql":
        backend.run("DENY SELECT ON sales.customer TO reader;")
    else:
        backend.run("REVOKE SELECT ON sales.customer FROM reader;")
    try:
        error = pytest.raises(ConnectorError, service.execute_sql, owner, cid, "SELECT id FROM sales.customer").value
        assert error.code in ("permission_denied", "query_failed") and ext.SECRET_ROW not in repr(vars(error))
    finally:
        backend.run(
            "REVOKE SELECT ON sales.customer FROM reader;"
            if kind(backend) == "mssql"
            else "GRANT SELECT ON sales.customer TO reader;"
        )
    assert sessions(backend) == 0


def test_a_query_the_database_does_not_take_is_a_fixed_code_without_its_text(backend, service, owner):
    cid = pick(service, owner, backend, [table("sales", "customer")])
    error = pytest.raises(
        ConnectorError, service.execute_sql, owner, cid, "SELECT CAST(c.name AS integer) FROM sales.customer c"
    ).value
    assert (error.code, error.status) == ("query_failed", 422)
    assert "Hidden" not in repr(vars(error)) and error.__cause__ is None


def test_nothing_the_validator_refuses_reaches_the_server(backend, service, owner):
    cid = pick(service, owner, backend, [table("sales", "customer")])
    before = backend.server.checksum()
    for sql in (
        "DELETE FROM sales.customer",
        "DROP TABLE sales.customer",
        "SELECT 1; DELETE FROM sales.customer",
        "SELECT * INTO sales.copy FROM sales.customer",
    ):
        assert pytest.raises(ConnectorError, service.execute_sql, owner, cid, sql).value.code == "sql_rejected"
    assert backend.server.checksum() == before and sessions(backend) == 0


def test_the_external_database_can_be_run_against_repeatedly_without_leaking_sessions(backend, service, owner):
    cid = pick(service, owner, backend, [table("sales", "customer")])
    for _ in range(15):
        service.execute_sql(owner, cid, "SELECT id FROM sales.customer")
    assert sessions(backend) == 0


# ---------- security audit: every way to hold a write permission is refused before anything runs ----------

# name -> statements an administrator runs after `{user}` has been given nothing but SELECT on sales.customer
PG_WRITERS = {
    "column_update": ["GRANT UPDATE (name) ON sales.customer TO {user};"],
    "column_insert": ["GRANT INSERT (id) ON sales.customer TO {user};"],
    "view_insert": ["GRANT INSERT ON sales.big_orders TO {user};"],
    "database_create": ["GRANT CREATE ON DATABASE ext TO {user};"],
    "write_server_files": ["GRANT pg_write_server_files TO {user};"],
    "server_program": ["GRANT pg_execute_server_program TO {user};"],
    "read_server_files": ["GRANT pg_read_server_files TO {user};"],
    "owner": [
        "CREATE TABLE sales.zz_owned (a int); ALTER TABLE sales.zz_owned OWNER TO {user};",
        "REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON sales.zz_owned FROM {user};",
    ],
    "sequence": ["CREATE SEQUENCE sales.zz_seq; GRANT UPDATE ON sales.zz_seq TO {user};"],
    "noinherit_superuser": [
        "CREATE ROLE zz_boss SUPERUSER NOLOGIN; GRANT zz_boss TO {user}; ALTER ROLE {user} NOINHERIT;"
    ],
    "noinherit_writer": [
        "CREATE ROLE zz_clerk NOLOGIN; GRANT USAGE ON SCHEMA sales TO zz_clerk;",
        "GRANT DELETE ON sales.customer TO zz_clerk; ALTER ROLE {user} NOINHERIT; GRANT zz_clerk TO {user};",
    ],
    "search_path_spoof": [
        "CREATE SCHEMA zz_trap; GRANT USAGE ON SCHEMA zz_trap TO {user};",
        "CREATE VIEW zz_trap.pg_roles AS SELECT * FROM pg_catalog.pg_roles WHERE false;",
        "CREATE VIEW zz_trap.pg_class AS SELECT * FROM pg_catalog.pg_class WHERE false;",
        "CREATE VIEW zz_trap.pg_namespace AS SELECT * FROM pg_catalog.pg_namespace WHERE false;",
        "GRANT SELECT ON ALL TABLES IN SCHEMA zz_trap TO {user};",
        "GRANT DELETE ON sales.customer TO {user}; ALTER ROLE {user} SET search_path = zz_trap, pg_catalog;",
    ],
}
PG_WRITER_CLEANUP = (
    "DROP TABLE IF EXISTS sales.zz_owned; DROP SEQUENCE IF EXISTS sales.zz_seq; DROP SCHEMA IF EXISTS zz_trap CASCADE;"
    " DROP OWNED BY {user}; DROP ROLE {user}; DROP ROLE IF EXISTS zz_boss;"
)
PG_WRITER_CLEANUP_CLERK = " DROP OWNED BY zz_clerk; DROP ROLE zz_clerk;"

# the same for SQL Server; a statement that starts with "master:" is run in the master database
MSSQL_WRITERS = {
    "column_update": ["GRANT UPDATE ON sales.customer (name) TO {user}"],
    "empty_schema_alter": ["CREATE SCHEMA zz_{user}", "GRANT ALTER ON SCHEMA::zz_{user} TO {user}"],
    "empty_schema_insert": ["CREATE SCHEMA zz_{user}", "GRANT INSERT ON SCHEMA::zz_{user} TO {user}"],
    "impersonate_a_writer": ["GRANT IMPERSONATE ON USER::writer TO {user}"],
    "create_synonym": ["GRANT CREATE SYNONYM TO {user}"],
    "ddl_trigger": ["GRANT ALTER ANY DATABASE DDL TRIGGER TO {user}"],
    "sequence": ["CREATE SEQUENCE sales.zz_{user} AS int", "GRANT UPDATE ON sales.zz_{user} TO {user}"],
    "procedure": ["CREATE PROCEDURE sales.zz_{user} AS SELECT 1", "GRANT EXECUTE ON sales.zz_{user} TO {user}"],
    "database_execute": ["GRANT EXECUTE TO {user}"],
    "datawriter": ["ALTER ROLE db_datawriter ADD MEMBER {user}"],
    "custom_role": [
        "CREATE ROLE zz_role_{user}",
        "GRANT DELETE ON sales.customer TO zz_role_{user}",
        "ALTER ROLE zz_role_{user} ADD MEMBER {user}",
    ],
    "write_granted_to_public": ["GRANT UPDATE ON sales.order_note TO public"],
    "server_alter_login": ["master:GRANT ALTER ANY LOGIN TO {user}"],
    "server_securityadmin": ["ALTER SERVER ROLE securityadmin ADD MEMBER {user}"],
    "server_dbcreator": ["ALTER SERVER ROLE dbcreator ADD MEMBER {user}"],
}
MSSQL_UNDO = {"write_granted_to_public": "REVOKE UPDATE ON sales.order_note FROM public"}
MSSQL_READERS = {
    "one_table": [],
    "datareader": ["ALTER ROLE db_datareader ADD MEMBER {user}"],
    "view_definition": ["GRANT VIEW DEFINITION TO {user}"],
}


def make_account(backend, user, statements):
    password = ext.READER_PASSWORD
    if kind(backend) == "mssql":
        backend.run(f"CREATE LOGIN {user} WITH PASSWORD = '{password}', CHECK_POLICY = OFF")
        backend.run(f"CREATE USER {user} FOR LOGIN {user}")
        backend.run(f"GRANT SELECT ON sales.customer TO {user}")
        for statement in statements:
            text = statement.format(user=user)
            if text.startswith("master:"):
                backend.server.script(f"USE master;\nGO\n{text[7:]};\nGO\n")
            else:
                backend.run(text)
    else:
        backend.run(
            f"CREATE ROLE {user} LOGIN PASSWORD '{password}'; GRANT USAGE ON SCHEMA sales TO {user}; "
            f"GRANT SELECT ON sales.customer TO {user};"
        )
        for statement in statements:
            backend.run(statement.format(user=user))
    return password


def test_every_kind_of_write_permission_makes_the_account_refused(backend):
    mssql = kind(backend) == "mssql"
    accepted = []
    for number, (case, statements) in enumerate((MSSQL_WRITERS if mssql else PG_WRITERS).items()):
        user = f"zz_aud{number}"
        password = make_account(backend, user, statements)
        try:
            target, _, driver = target_of(backend, user, password)
            try:
                driver.run(target, password, "SELECT 1", limits())
                accepted.append(case)
            except DriverError as error:
                if error.code != "account_can_write":
                    accepted.append(f"{case}: {error.code}")
        finally:
            if not mssql:
                cleanup = PG_WRITER_CLEANUP.format(user=user)
                backend.run(cleanup + (PG_WRITER_CLEANUP_CLERK if case == "noinherit_writer" else ""))
            elif case in MSSQL_UNDO:
                backend.run(MSSQL_UNDO[case])
    assert accepted == [], accepted


def test_select_only_accounts_are_still_accepted(backend):
    mssql = kind(backend) == "mssql"
    cases = MSSQL_READERS if mssql else {"one_table": []}
    for number, statements in enumerate(cases.values()):
        user = f"zz_read{number}"
        password = make_account(backend, user, statements)
        try:
            target, _, driver = target_of(backend, user, password)
            assert driver.run(target, password, "SELECT 1", limits()).rows == [[1]], statements
            assert driver.test(target, password, 10).can_write is False, statements
        finally:
            if not mssql:
                backend.run(f"DROP OWNED BY {user}; DROP ROLE {user};")
    target, password, driver = target_of(backend)
    assert driver.run(target, password, "SELECT 1", limits()).rows == [[1]]


def test_a_permission_check_without_a_clear_answer_runs_nothing(backend, monkeypatch):
    """Fail closed: a check that returns NULL (or no row) must not be read as "this account cannot write"."""
    target, password, driver = target_of(backend)
    name = "CAN_CHANGE_ANYTHING" if kind(backend) == "mssql" else "CAN_WRITE"
    for answer in ("SELECT NULL", "SELECT 1 WHERE 1 = 0"):
        monkeypatch.setattr(driver, name, answer)
        with pytest.raises(DriverError) as error:
            driver.run(target, password, "SELECT 1", limits())
        assert error.value.code == "account_can_write", answer


def test_a_run_cancelled_before_it_starts_never_sends_the_query(backend):
    target, password, driver = target_of(backend)
    token = CancelToken()
    token.cancel()
    sleeper = "SELECT pg_sleep(20)" if kind(backend) == "pg" else "WAITFOR DELAY '00:00:20'; SELECT 1"
    started = time.monotonic()
    with pytest.raises(DriverError) as error:
        driver.run(target, password, sleeper, limits(), token)
    assert error.value.code == "cancelled" and time.monotonic() - started < 8
    assert wait_until(lambda: sessions(backend) == 0)


HIDDEN = {"kind": "column", "schema": "sales", "object": "customer", "column": "secret_note", "mode": "exclude"}


def test_the_accounts_string_settings_cannot_change_what_the_canonical_text_means(backend, service, owner):
    """PostgreSQL with standard_conforming_strings=off reads a backslash inside '...' as an escape: the end of a literal
    would move and its content would run as SQL (here: a hidden column, through a subquery)."""
    if kind(backend) == "mssql":
        pytest.skip("SQL Server has no such setting")
    cid = pick(service, owner, backend, [table("sales", "customer"), HIDDEN])
    backend.run("ALTER ROLE reader SET standard_conforming_strings = off;")
    try:
        result = service.execute_sql(
            owner, cid, "SELECT 'a\\' AS x, ' , (SELECT secret_note FROM sales.customer) AS leak --' AS y"
        )
        assert [c["name"] for c in result["columns"]] == ["x", "y"]
        assert ext.SECRET_ROW not in str(result["rows"])
        assert result["rows"][0][0] == "a\\"
    finally:
        backend.run("ALTER ROLE reader RESET standard_conforming_strings;")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT c FROM sales.customer c",
        "SELECT concat(c.*) FROM sales.customer c",
        "SELECT array_agg(c) FROM sales.customer c",
        "SELECT c.id FROM sales.customer c WHERE CAST(c AS varchar(500)) LIKE '%hidden%'",
    ],
)
def test_a_hidden_column_cannot_be_read_through_the_whole_row(backend, service, owner, sql):
    cid = pick(service, owner, backend, [table("sales", "customer"), HIDDEN])
    error = pytest.raises(ConnectorError, service.execute_sql, owner, cid, sql).value
    assert error.code == "sql_rejected"


def test_limits_hold_for_nested_queries_ctes_and_the_queries_own_limit_clauses(backend, service, owner):
    mssql = kind(backend) == "mssql"
    with_big = BIG[kind(backend)]
    backend.run(with_big[0])
    try:
        cid = pick(service, owner, backend, [table("sales", "zz_big")])
        queries = [
            "WITH a AS (SELECT n FROM sales.zz_big), b AS (SELECT n FROM a) SELECT n FROM b",
            "SELECT q.n FROM (SELECT n FROM sales.zz_big) q",
            "SELECT z.n FROM sales.zz_big z ORDER BY z.n",
        ]
        if mssql:
            queries += [
                "SELECT TOP 100000 n FROM sales.zz_big",
                "SELECT TOP 100 PERCENT n FROM sales.zz_big",
                "SELECT TOP (100000) WITH TIES n FROM sales.zz_big ORDER BY n",
                "SELECT n FROM sales.zz_big ORDER BY n OFFSET 0 ROWS FETCH NEXT 100000 ROWS ONLY",
                "SELECT n FROM sales.zz_big ORDER BY n OFFSET 5 ROWS",
            ]
        else:
            queries += [
                "SELECT n FROM sales.zz_big LIMIT 100000",
                "SELECT n FROM sales.zz_big LIMIT 1 + 100000",
                "SELECT n FROM sales.zz_big FETCH FIRST 100000 ROWS ONLY",
                "SELECT n FROM sales.zz_big ORDER BY n OFFSET 5",
                "(SELECT n FROM sales.zz_big)",
            ]
        for sql in queries:
            result = service.execute_sql(owner, cid, sql, max_rows=25)
            assert result["row_count"] == 25 and result["truncated_by"] == "rows", sql
        assert wait_until(lambda: sessions(backend) == 0)
    finally:
        backend.run(with_big[1])
