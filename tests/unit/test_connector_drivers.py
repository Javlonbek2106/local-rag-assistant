"""M4.1b: what the drivers turn their failures into. A driver's own text (it may hold a connection string, a
user name, a
password) is looked at only to choose a fixed code, and is never kept."""

import socket

import psycopg
import pytest
from psycopg import errors

from rag.connector import drivers
from rag.connector.drivers import Target, mssql, postgres
from rag.connector.errors import DRIVER_CODES, ConnectorError, DriverError

SECRET = "Sentinel-Pa55word!-zq81"


# ---------- PostgreSQL ----------


@pytest.mark.parametrize(
    "error,code",
    [
        (errors.InvalidPassword("password authentication failed for user reader"), "auth_failed"),
        (errors.InvalidAuthorizationSpecification("no pg_hba.conf entry"), "auth_failed"),
        (errors.InvalidCatalogName('database "nodb" does not exist'), "database_not_found"),
        (errors.QueryCanceled("canceling statement due to statement timeout"), "timeout"),
        (errors.InsufficientPrivilege("permission denied for table x"), "permission_denied"),
        (
            psycopg.OperationalError('connection failed: FATAL:  password authentication failed for user "reader"'),
            "auth_failed",
        ),
        (psycopg.OperationalError('connection failed: FATAL:  role "nobody" does not exist'), "auth_failed"),
        (psycopg.OperationalError('connection failed: FATAL:  database "nodb" does not exist'), "database_not_found"),
        (
            psycopg.OperationalError(
                'FATAL: no pg_hba.conf entry for host "1.2.3.4", user "u", database "d", no encryption'
            ),
            "auth_failed",
        ),
        (psycopg.OperationalError("connection timeout expired"), "timeout"),
        (psycopg.OperationalError("SSL error: certificate verify failed"), "tls_failed"),
        (
            psycopg.OperationalError('connection to server at "10.0.0.5", port 5432 failed: Connection refused'),
            "unreachable",
        ),
        (psycopg.ProgrammingError("something odd"), "failed"),
    ],
)
def test_postgres_failures_become_fixed_codes(error, code):
    assert postgres.classify(error) == code and code in DRIVER_CODES


def test_postgres_never_lets_the_driver_text_out(monkeypatch):
    target = Target("db.example.org", ("93.184.216.34",), 5432, "sales", "reader", "disable")

    def boom(**kwargs):
        raise psycopg.OperationalError(
            f'connection to server failed: FATAL: password "{SECRET}" rejected for user "reader"'
        )

    monkeypatch.setattr(psycopg, "connect", boom)
    with pytest.raises(DriverError) as error:
        postgres.test(target, SECRET, 3)
    assert error.value.code == "unreachable" and SECRET not in repr(error.value) and error.value.__cause__ is None
    assert error.value.__suppress_context__ is True


def test_postgres_connects_with_separate_parameters_a_pinned_address_and_read_only_session(monkeypatch):
    """(3 s for two addresses is 1 s per address: all of them together stay within the bound)"""
    seen = {}

    class Stop(Exception):
        pass

    def fake(**kwargs):
        seen.update(kwargs)
        raise psycopg.OperationalError("stop")

    monkeypatch.setattr(psycopg, "connect", fake)
    hostile = Target(
        "db.example.org", ("93.184.216.34", "10.0.0.7"), 5432, "x sslmode=disable host=evil", "u'; --", "verify-full"
    )
    with pytest.raises(DriverError):
        postgres.test(hostile, "p w;d", 3)
    assert seen["host"] == "db.example.org,db.example.org" and seen["hostaddr"] == "93.184.216.34,10.0.0.7"
    assert seen["port"] == "5432,5432"
    assert seen["dbname"] == "x sslmode=disable host=evil" and seen["user"] == "u'; --" and seen["password"] == "p w;d"
    assert seen["sslmode"] == "verify-full" and seen["sslrootcert"] == "system" and seen["connect_timeout"] == 1
    assert "default_transaction_read_only=on" in seen["options"]
    assert set(seen) == {
        "host",
        "hostaddr",
        "port",
        "dbname",
        "user",
        "password",
        "connect_timeout",
        "application_name",
        "options",
        "sslmode",
        "sslrootcert",
    }  # nothing a person typed became another setting


def test_postgres_catalog_queries_are_fixed_text_and_select_only():
    for sql in (postgres.OBJECTS, postgres.COLUMNS, postgres.CONSTRAINTS, postgres.INDEXES, postgres.CAN_WRITE):
        assert sql.lstrip().upper().startswith("SELECT") and ";" not in sql
        for word in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ", "CREATE ", "TRUNCATE ", "GRANT "):
            assert word not in sql.upper().replace("CREATE'", "").replace("CREATE,", "")
    assert "pg_catalog" in postgres.SYSTEM_SCHEMAS and "information_schema" in postgres.SYSTEM_SCHEMAS


# ---------- SQL Server ----------


class Error(Exception):
    def __init__(self, text, number=None):
        super().__init__(text)
        self.number = number


@pytest.mark.parametrize(
    "error,code",
    [
        (Error("Login failed for user 'reader'.", 18456), "auth_failed"),
        (Error('Cannot open database "nodb" requested by the login. The login failed.', 18456), "database_not_found"),
        (Error("database unavailable", 4060), "database_not_found"),
        (Error("The SELECT permission was denied", 229), "permission_denied"),
        (TimeoutError("timed out"), "timeout"),
        (ConnectionRefusedError("refused"), "unreachable"),
        (socket.gaierror("no such host"), "unreachable"),
        (Error("SSL handshake failure"), "tls_failed"),
        (RuntimeError("something else"), "failed"),
    ],
)
def test_sql_server_failures_become_fixed_codes(error, code):
    assert mssql.classify(error) == code and code in DRIVER_CODES


def test_sql_server_tls_needs_a_ca_and_never_falls_back_to_plain_text():
    pytest.importorskip("pytds")  # (without the driver the answer is "driver_missing": equally no connection)
    target = Target("db.example.org", ("93.184.216.34",), 1433, "sales", "reader", "verify-full", None)
    with pytest.raises(DriverError) as error:
        mssql.test(target, SECRET, 3)
    assert error.value.code == "tls_failed"


def test_sql_server_without_its_driver_says_so(monkeypatch):
    import builtins

    real = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "pytds":
            raise ImportError(name)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    with pytest.raises(DriverError) as error:
        mssql.test(Target("h", ("93.184.216.34",), 1433, "d", "u", "disable"), SECRET, 3)
    assert error.value.code == "driver_missing"


def test_sql_server_catalog_queries_are_fixed_text_and_select_only():
    for sql in (
        mssql.OBJECTS,
        mssql.COLUMNS,
        mssql.KEYS,
        mssql.FOREIGN_KEYS,
        mssql.INDEXES,
        mssql.CAN_CHANGE_ANYTHING,
        mssql.VERSION,
    ):
        assert sql.lstrip().upper().startswith("SELECT") and ";" not in sql
    assert "'sys'" in mssql.USER_OBJECTS and "INFORMATION_SCHEMA" in mssql.USER_OBJECTS


def test_grouping_of_ordered_rows_keeps_one_key_at_a_time():
    rows = [("s", "t", "pk", "a"), ("s", "t", "pk", "b"), ("s", "u", "pk2", "c")]
    assert list(mssql._grouped(iter(rows), 3)) == [(("s", "t", "pk"), [("a",), ("b",)]), (("s", "u", "pk2"), [("c",)])]
    assert list(mssql._grouped(iter([]), 3)) == []


# ---------- the lookup ----------


def test_a_provider_is_found_by_name_and_an_unknown_one_is_refused():
    assert drivers.driver("postgresql") is postgres and drivers.driver("mssql") is mssql
    with pytest.raises(ConnectorError) as error:
        drivers.driver("oracle")
    assert error.value.code == "invalid_provider"
