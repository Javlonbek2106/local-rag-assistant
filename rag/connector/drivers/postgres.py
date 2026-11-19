"""PostgreSQL adapter (driver: psycopg 3, already a dependency).

Read-only in three independent ways: the session is opened with default_transaction_read_only, the transaction is a
READ ONLY one (psycopg `read_only`), and the only statements ever sent are `SELECT 1`, `SHOW`, and catalog queries
that THIS module wrote (no text from a person, no parameter that is not a number). It never reads a row of a user
table.

Every value that identifies the server (name, port, database, user, password) is a separate parameter, never part of a
connection string, so none of them can add a setting. The address was already judged (network.py) and is connected to
directly (`hostaddr`); the name is kept only for the certificate check.
"""

import contextlib
import tempfile
import time
from pathlib import Path

import psycopg

from rag.connector.drivers import Introspection, TestResult
from rag.connector.errors import DriverError

SYSTEM_SCHEMAS = (
    "n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg\\_toast%' "
    "AND n.nspname NOT LIKE 'pg\\_temp\\_%'"
)
RELATIONS = f"c.relkind IN ('r', 'p', 'v', 'm', 'f') AND NOT c.relispartition AND {SYSTEM_SCHEMAS}"
KINDS = {"r": "table", "p": "table", "f": "table", "v": "view", "m": "materialized_view"}
CONSTRAINT_KINDS = {"p": "primary_key", "u": "unique", "f": "foreign_key"}
FETCH = 1000

OBJECTS = (
    "SELECT n.nspname, c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    f"WHERE {RELATIONS} ORDER BY n.nspname, c.relname"
)
COLUMNS = (
    "SELECT n.nspname, c.relname, a.attnum, a.attname, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull "
    "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
    f"WHERE a.attnum > 0 AND NOT a.attisdropped AND {RELATIONS} ORDER BY n.nspname, c.relname, a.attnum"
)
CONSTRAINTS = (
    "SELECT n.nspname, c.relname, k.contype, k.conname, "
    "ARRAY(SELECT a.attname FROM unnest(k.conkey) WITH ORDINALITY u(attnum, ord) JOIN pg_attribute a "
    "ON a.attrelid = k.conrelid AND a.attnum = u.attnum ORDER BY u.ord), rn.nspname, rc.relname, "
    "ARRAY(SELECT a.attname FROM unnest(k.confkey) WITH ORDINALITY u(attnum, ord) JOIN pg_attribute a "
    "ON a.attrelid = k.confrelid AND a.attnum = u.attnum ORDER BY u.ord) "
    "FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
    "LEFT JOIN pg_class rc ON rc.oid = k.confrelid LEFT JOIN pg_namespace rn ON rn.oid = rc.relnamespace "
    f"WHERE k.contype IN ('p', 'u', 'f') AND {RELATIONS} ORDER BY n.nspname, c.relname, k.conname"
)
INDEXES = (
    "SELECT n.nspname, c.relname, ic.relname, "
    "ARRAY(SELECT a.attname FROM unnest(i.indkey::int2[]) WITH ORDINALITY u(attnum, ord) JOIN pg_attribute a "
    "ON a.attrelid = i.indrelid AND a.attnum = u.attnum WHERE u.attnum > 0 ORDER BY u.ord), i.indisunique, "
    "i.indisprimary FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid JOIN pg_class ic ON ic.oid = i.indexrelid "
    f"JOIN pg_namespace n ON n.oid = c.relnamespace WHERE {RELATIONS} ORDER BY n.nspname, c.relname, ic.relname"
)
# Can the account change anything? Superuser or role-creating, may create objects in a user schema, or may write to a
# table.
CAN_WRITE = (
    "SELECT EXISTS (SELECT 1 FROM pg_roles r WHERE r.rolname = current_user AND (r.rolsuper OR r.rolcreatedb "
    "OR r.rolcreaterole)) OR EXISTS (SELECT 1 FROM pg_namespace n WHERE "
    + SYSTEM_SCHEMAS
    + " AND has_schema_privilege(n.oid, 'CREATE')) OR EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n "
    "ON n.oid = c.relnamespace WHERE c.relkind IN ('r', 'p') AND "
    + SYSTEM_SCHEMAS
    + " AND (has_table_privilege(c.oid, 'INSERT') OR has_table_privilege(c.oid, 'UPDATE') "
    "OR has_table_privilege(c.oid, 'DELETE')))"
)


def classify(error):
    """A fixed code for what went wrong. The text of the error is looked at here and nowhere else, and is not kept."""
    state = getattr(error, "sqlstate", None)
    if state in ("28P01", "28000", "28"):
        return "auth_failed"
    if state == "3D000":
        return "database_not_found"
    if state in ("57014",):
        return "timeout"
    if state in ("42501",):
        return "permission_denied"
    # (a failure while connecting carries no SQLSTATE: the server's FATAL line is the only clue)
    text = str(error).lower()
    if (
        "password authentication failed" in text
        or "no pg_hba.conf entry" in text
        or ("role" in text and "does not exist" in text)
        or "authentication failed" in text
    ):
        return "auth_failed"
    if "database" in text and "does not exist" in text:
        return "database_not_found"
    # (before "timeout": a list of failed attempts may hold both)
    if "ssl" in text or "certificate" in text or "tls" in text:
        return "tls_failed"
    if "timeout expired" in text or "timed out" in text:
        return "timeout"
    if isinstance(error, psycopg.OperationalError) and not state:
        return "unreachable"
    return "failed"


@contextlib.contextmanager
def _connection(target, password, timeout, read_only=True):
    with contextlib.ExitStack() as stack:
        ssl = {"sslmode": target.ssl_mode}
        if target.ssl_mode in ("verify-ca", "verify-full"):
            if target.ca_pem:
                folder = stack.enter_context(tempfile.TemporaryDirectory())
                path = Path(folder) / "ca.pem"
                path.write_text(target.ca_pem, encoding="utf-8")
                ssl["sslrootcert"] = str(path)
            else:
                ssl["sslrootcert"] = "system"
        try:
            count = len(target.addresses)  # libpq tries host/hostaddr/port pairs in order: one pair per address
            conn = psycopg.connect(
                host=",".join([target.host] * count),
                hostaddr=",".join(target.addresses),
                port=",".join([str(target.port)] * count),
                dbname=target.database,
                # per address: all of them together stay within the timeout
                user=target.user,
                password=password,
                connect_timeout=max(1, int(timeout / count)),
                application_name="rag-connector",
                options="-c default_transaction_read_only=on -c idle_in_transaction_session_timeout=60000",
                **ssl,
            )
        except psycopg.Error as error:
            raise DriverError(classify(error)) from None
        stack.callback(conn.close)
        conn.read_only = read_only
        yield conn
        conn.rollback()


def _remaining(deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise DriverError("timeout")
    return left


def test(target, password, timeout):
    deadline = time.monotonic() + timeout
    try:
        with _connection(target, password, timeout) as conn:
            conn.execute(f"SET LOCAL statement_timeout = {int(_remaining(deadline) * 1000)}")
            conn.execute("SELECT 1")
            version = int(conn.execute("SHOW server_version_num").fetchone()[0])
            writable = bool(conn.execute(CAN_WRITE).fetchone()[0])
    except DriverError:
        raise
    except psycopg.Error as error:
        raise DriverError(classify(error)) from None
    return TestResult(
        version=f"{version // 10000}.{version % 10000 // 100}" if version >= 100000 else str(version),
        can_write=writable,
    )


def introspect(target, password, sink, timeout):
    deadline = time.monotonic() + timeout
    try:
        with _connection(target, password, min(timeout, 15)) as conn:

            def stream(sql, name):
                conn.execute(f"SET LOCAL statement_timeout = {int(_remaining(deadline) * 1000)}")
                with conn.cursor(name=name) as cursor:  # server-side: rows arrive in batches, not all at once
                    cursor.itersize = FETCH
                    cursor.execute(sql)
                    for row in cursor:
                        _remaining(deadline)
                        yield row

            for schema, name, kind in stream(OBJECTS, "objects"):
                sink.object(schema, name, KINDS[kind])
            for schema, name, ordinal, column, data_type, nullable in stream(COLUMNS, "columns"):
                sink.column(schema, name, ordinal, column, data_type, nullable)
            for schema, name, kind, constraint, columns, ref_schema, ref_object, ref_columns in stream(
                CONSTRAINTS, "constraints"
            ):
                if kind == "f":
                    sink.constraint(
                        schema, name, "foreign_key", constraint, columns, ref_schema, ref_object, ref_columns
                    )
                else:
                    sink.constraint(schema, name, CONSTRAINT_KINDS[kind], constraint, columns)
            for schema, name, index, columns, unique, primary in stream(INDEXES, "indexes"):
                sink.index(schema, name, index, columns, unique, primary)
    except DriverError:
        raise
    except psycopg.Error as error:
        raise DriverError(classify(error)) from None
    return Introspection()
