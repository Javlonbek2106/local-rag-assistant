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
import threading
import time
from pathlib import Path

import psycopg

from rag.connector.drivers import Introspection, QueryResult, TestResult, cell, row_size
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
# Can the account change anything? Asked about every role it is a member of, directly or indirectly, also through a
# NOINHERIT membership: a role with a powerful attribute, or a predefined role that writes or reaches the server's
# files or programs; owning the database, a user schema, a relation or a function; creating in the database or in a user
# schema; writing to a table, view or foreign table (also to one column of it); advancing or setting a sequence.
# TEMPORARY (every account has it by default) is not counted. It must only run with `search_path = pg_catalog`
# (_pin_session): otherwise the account's own search_path decides what `pg_roles` and `has_table_privilege` mean.
MINE = "(SELECT r.oid FROM pg_roles r WHERE pg_has_role(current_user, r.oid, 'MEMBER')) AS m"
CAN_WRITE = (
    "SELECT EXISTS (SELECT 1 FROM pg_roles r WHERE pg_has_role(current_user, r.oid, 'MEMBER') AND (r.rolsuper "
    "OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication OR r.rolname IN ('pg_write_all_data', "
    "'pg_write_server_files', 'pg_read_server_files', 'pg_execute_server_program', 'pg_signal_backend', "
    "'pg_maintain', 'pg_checkpoint', 'pg_create_subscription'))) "
    f"OR EXISTS (SELECT 1 FROM {MINE} CROSS JOIN pg_database d WHERE d.datname = current_database() "
    "AND (d.datdba = m.oid OR has_database_privilege(m.oid, d.oid, 'CREATE'))) "
    f"OR EXISTS (SELECT 1 FROM {MINE} CROSS JOIN pg_namespace n WHERE {SYSTEM_SCHEMAS} "
    "AND (n.nspowner = m.oid OR has_schema_privilege(m.oid, n.oid, 'CREATE'))) "
    f"OR EXISTS (SELECT 1 FROM {MINE} CROSS JOIN pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    f"WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S') AND {SYSTEM_SCHEMAS} AND (c.relowner = m.oid OR CASE "
    "WHEN c.relkind = 'S' THEN has_sequence_privilege(m.oid, c.oid, 'USAGE, UPDATE') "
    "ELSE has_table_privilege(m.oid, c.oid, 'INSERT, UPDATE, DELETE, TRUNCATE') "
    "OR has_any_column_privilege(m.oid, c.oid, 'INSERT, UPDATE') END)) "
    f"OR EXISTS (SELECT 1 FROM {MINE} CROSS JOIN pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
    f"WHERE {SYSTEM_SCHEMAS} AND p.proowner = m.oid)"
)
# A query run asks for its rows in small batches: a whole batch is in memory before its values are cut.
RUN_FETCH = 20


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
    if state and state[:2] in ("22", "42", "21", "0A", "54", "25"):  # the query itself: data, name, syntax, size, state
        return "query_error"
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


def _pin_session(conn, timeout_s):
    """What the statements after this one mean must not depend on the account's or the database's own settings, and none
    may run longer than `timeout_s`. search_path: names resolve in pg_catalog only. standard_conforming_strings: with it
    off a backslash inside '...' is an escape, so the end of a literal moves and its content would run as SQL."""
    conn.execute(f"SET LOCAL statement_timeout = {max(1, int(timeout_s * 1000))}")
    conn.execute("SET LOCAL lock_timeout = 5000")
    conn.execute("SET LOCAL search_path = pg_catalog")
    conn.execute("SET LOCAL standard_conforming_strings = on")


def _can_write(conn):
    """Anything but a clear "no" counts as "yes"."""
    row = conn.execute(CAN_WRITE).fetchone()
    return row is None or row[0] is not False


def _remaining(deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise DriverError("timeout")
    return left


def test(target, password, timeout):
    deadline = time.monotonic() + timeout
    try:
        with _connection(target, password, timeout) as conn:
            _pin_session(conn, _remaining(deadline))
            conn.execute("SELECT 1")
            version = int(conn.execute("SHOW server_version_num").fetchone()[0])
            writable = _can_write(conn)
    except DriverError:
        raise
    except psycopg.Error as error:
        raise DriverError(classify(error)) from None
    return TestResult(
        version=f"{version // 10000}.{version % 10000 // 100}" if version >= 100000 else str(version),
        can_write=writable,
    )


def _column_type(conn, oid):
    try:
        info = conn.adapters.types.get(oid)
        return info.name if info else ""
    except Exception:  # noqa: BLE001
        return ""


def run(target, password, sql, limits, cancel=None):
    """Run ONE query that the validator (sqlcheck.py) wrote and return its bounded result. Never called with a text
    that did not come from there: this module does not look at the SQL.

    The protections, each independent of the others:
    - the account must not be able to change anything (CAN_WRITE, checked on this very connection, every time):
      otherwise `account_can_write` and nothing is run;
    - the session and the transaction are READ ONLY;
    - before anything else, also before the account check, `search_path` is set to `pg_catalog` only (the validator
      writes every table as schema.name) and standard_conforming_strings to on (_pin_session);
    - `statement_timeout` for the server, a timer that sends a cancel request, and a deadline between batches;
    - rows are read through a server-side cursor in batches, and reading stops at the row or byte limit (the cursor is
      dropped with the connection: the server does not go on computing what nobody reads);
    - everything is rolled back, the connection closed."""
    started = time.monotonic()
    try:
        with _connection(target, password, limits.connect_timeout_s) as conn:
            timer = None
            if cancel:
                cancel.attach(conn.cancel_safe)

            def stop_if_cancelled():  # a cancel request sent between two statements stops nothing by itself
                if cancel and cancel.cancelled:
                    raise DriverError("cancelled")

            try:
                stop_if_cancelled()
                timer = threading.Timer(limits.timeout_s, conn.cancel_safe)
                timer.daemon = True
                timer.start()
                _pin_session(conn, limits.timeout_s)
                if _can_write(conn):
                    raise DriverError("account_can_write")
                stop_if_cancelled()
                columns, rows, truncated, size = [], [], None, 0
                with conn.cursor(name="rag_query") as cursor:
                    cursor.itersize = RUN_FETCH
                    cursor.execute(sql)
                    columns = [
                        {"name": c.name, "type": _column_type(conn, c.type_code)} for c in (cursor.description or [])
                    ]
                    while truncated is None:
                        stop_if_cancelled()
                        batch = cursor.fetchmany(RUN_FETCH)
                        if not batch:
                            break
                        if time.monotonic() - started > limits.timeout_s:
                            raise DriverError("timeout")
                        for raw in batch:
                            if len(rows) >= limits.max_rows:
                                truncated = "rows"
                                break
                            row = [cell(v, limits.max_cell_chars) for v in raw]
                            size += row_size(row)
                            if size > limits.max_bytes:
                                truncated = "bytes"
                                break
                            rows.append(row)
            finally:
                if timer:
                    timer.cancel()
                if cancel:
                    cancel.detach()
    except DriverError:
        raise
    except psycopg.Error as error:
        if cancel and cancel.cancelled:
            raise DriverError("cancelled") from None
        raise DriverError(classify(error)) from None
    return QueryResult(columns, rows, truncated, round(time.monotonic() - started, 3))


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
