"""Microsoft SQL Server adapter (driver: python-tds, pure Python; optional, requirements-connector.txt).

What can and cannot be promised here (docs/connector.md):
- SQL Server has no session-level "read only". The protection is the ACCOUNT (the connection test warns when it can
  write) and the fact that this module only sends `SELECT`s of its own on catalog views. `readonly` asks for
  ApplicationIntent=ReadOnly.
- python-tds does TLS only with a CA file (and pyOpenSSL): ssl_mode "disable" is plain TDS, "verify-ca" and
  "verify-full" are certificate-checked TLS (the CA certificate comes from the connection's `ca_certificate_pem`
  option), "require" (encrypt without checking) does not exist in this driver and is refused at validation.
- The address that was judged is connected to by this module and handed to the driver as an open socket; the name is
  kept only for the certificate check.
- Catalog views are filtered by SQL Server to what the account may see: a poor account sees a small schema, not an
  error.

Every value is a separate parameter; the only SQL sent is written in this file.
"""

import contextlib
import logging
import socket
import tempfile
import threading
import time
from pathlib import Path

from rag.connector.drivers import Introspection, QueryResult, TestResult, cell, row_size
from rag.connector.errors import DriverError

FETCH = 1000
USER_OBJECTS = "o.is_ms_shipped = 0 AND s.name NOT IN ('sys', 'INFORMATION_SCHEMA')"
OBJECTS = (
    "SELECT s.name, o.name, o.type FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id "
    f"WHERE o.type IN ('U', 'V') AND {USER_OBJECTS} ORDER BY s.name, o.name"
)
TYPE_TEXT = (
    "CASE WHEN t.name IN ('varchar', 'char', 'varbinary', 'binary', 'nvarchar', 'nchar') THEN t.name + '(' + "
    "CASE WHEN c.max_length = -1 THEN 'max' WHEN t.name IN ('nvarchar', 'nchar') THEN CAST(c.max_length / 2 AS "
    "varchar(10)) ELSE CAST(c.max_length AS varchar(10)) END + ')' "
    "WHEN t.name IN ('decimal', 'numeric') THEN t.name + '(' + CAST(c.precision AS varchar(5)) + ',' + "
    "CAST(c.scale AS varchar(5)) + ')' "
    "WHEN t.name IN ('datetime2', 'datetimeoffset', 'time') THEN t.name + '(' + CAST(c.scale AS varchar(5)) + ')' "
    "ELSE t.name END"
)
COLUMNS = (
    f"SELECT s.name, o.name, c.column_id, c.name, {TYPE_TEXT}, c.is_nullable FROM sys.columns c "
    "JOIN sys.objects o ON o.object_id = c.object_id JOIN sys.schemas s ON s.schema_id = o.schema_id "
    "JOIN sys.types t ON t.user_type_id = c.user_type_id "
    f"WHERE o.type IN ('U', 'V') AND {USER_OBJECTS} ORDER BY s.name, o.name, c.column_id"
)
KEYS = (
    "SELECT s.name, o.name, i.is_primary_key, i.name, col.name FROM sys.indexes i "
    "JOIN sys.objects o ON o.object_id = i.object_id JOIN sys.schemas s ON s.schema_id = o.schema_id "
    "JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
    "AND ic.is_included_column = 0 JOIN sys.columns col ON col.object_id = ic.object_id "
    "AND col.column_id = ic.column_id WHERE o.type = 'U' AND (i.is_primary_key = 1 OR i.is_unique_constraint = 1) "
    f"AND {USER_OBJECTS} ORDER BY s.name, o.name, i.name, ic.key_ordinal"
)
FOREIGN_KEYS = (
    "SELECT s.name, o.name, fk.name, pc.name, rs.name, ro.name, rc.name FROM sys.foreign_keys fk "
    "JOIN sys.objects o ON o.object_id = fk.parent_object_id JOIN sys.schemas s ON s.schema_id = o.schema_id "
    "JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id "
    "JOIN sys.columns pc ON pc.object_id = fkc.parent_object_id AND pc.column_id = fkc.parent_column_id "
    "JOIN sys.objects ro ON ro.object_id = fk.referenced_object_id JOIN sys.schemas rs ON rs.schema_id = ro.schema_id "
    "JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id AND rc.column_id = fkc.referenced_column_id "
    f"WHERE {USER_OBJECTS} ORDER BY s.name, o.name, fk.name, fkc.constraint_column_id"
)
INDEXES = (
    "SELECT s.name, o.name, i.is_unique, i.is_primary_key, i.name, col.name FROM sys.indexes i "
    "JOIN sys.objects o ON o.object_id = i.object_id JOIN sys.schemas s ON s.schema_id = o.schema_id "
    "JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
    "AND ic.is_included_column = 0 JOIN sys.columns col ON col.object_id = ic.object_id "
    "AND col.column_id = ic.column_id WHERE o.type IN ('U', 'V') AND i.type > 0 AND i.name IS NOT NULL "
    f"AND {USER_OBJECTS} ORDER BY s.name, o.name, i.name, ic.key_ordinal"
)
# Can the account change anything? Written the other way round: the answer is 0 ("it cannot") only when every part
# proves it, and 1 otherwise, also when a part answers NULL. Allowlists, not denylists: a permission that is not known
# to be read-only counts as a write: at the server, at the database, on a schema, on an object (columns included) and
# in any grant made to the account or to a role it is in (which also covers what the other parts do not list, such as
# IMPERSONATE on a user or a login, types, triggers and synonyms).
SERVER_READ = (
    "'CONNECT SQL', 'CONNECT', 'VIEW ANY DATABASE', 'VIEW ANY DEFINITION', 'VIEW SERVER STATE', "
    "'VIEW SERVER PERFORMANCE STATE', 'VIEW SERVER SECURITY STATE', 'VIEW ANY PERFORMANCE DEFINITION', "
    "'VIEW ANY SECURITY DEFINITION'"
)
OBJECT_READ = "'SELECT', 'VIEW DEFINITION', 'VIEW CHANGE TRACKING'"
DATABASE_READ = (
    f"'CONNECT', {OBJECT_READ}, 'SHOWPLAN', 'VIEW DATABASE STATE', 'VIEW DATABASE PERFORMANCE STATE', "
    "'VIEW DATABASE SECURITY STATE', 'VIEW PERFORMANCE DEFINITION', 'VIEW SECURITY DEFINITION', "
    "'VIEW ANY COLUMN MASTER KEY DEFINITION', 'VIEW ANY COLUMN ENCRYPTION KEY DEFINITION'"
)
SERVER_ROLES = ("sysadmin", "securityadmin", "serveradmin", "setupadmin", "processadmin", "diskadmin", "dbcreator")
DATABASE_ROLES = ("db_owner", "db_securityadmin", "db_accessadmin", "db_ddladmin", "db_datawriter", "db_backupoperator")
NOT_IN_A_ROLE = " AND ".join(
    [f"ISNULL(IS_SRVROLEMEMBER('{role}'), 1) = 0" for role in (*SERVER_ROLES, "bulkadmin")]
    + [f"ISNULL(IS_MEMBER('{role}'), 1) = 0" for role in DATABASE_ROLES]
)
CAN_CHANGE_ANYTHING = (
    f"SELECT CASE WHEN {NOT_IN_A_ROLE} "
    "AND EXISTS (SELECT 1 FROM sys.fn_my_permissions(NULL, 'DATABASE') WHERE permission_name = 'CONNECT') "
    "AND NOT EXISTS (SELECT 1 FROM sys.fn_my_permissions(NULL, 'SERVER') "
    f"WHERE permission_name NOT IN ({SERVER_READ})) "
    "AND NOT EXISTS (SELECT 1 FROM sys.fn_my_permissions(NULL, 'DATABASE') "
    f"WHERE permission_name NOT IN ({DATABASE_READ})) "
    # (schema ids from 16384 on belong to the fixed database roles: a member of db_datareader "controls" the empty
    # schema of that name; an object put into one is still judged by the next part)
    "AND NOT EXISTS (SELECT 1 FROM sys.schemas s "
    "CROSS APPLY sys.fn_my_permissions(QUOTENAME(s.name), 'SCHEMA') p "
    "WHERE s.schema_id < 16384 AND s.name NOT IN ('sys', 'INFORMATION_SCHEMA') "
    f"AND p.permission_name NOT IN ({OBJECT_READ})) "
    "AND NOT EXISTS (SELECT 1 FROM sys.objects o CROSS APPLY sys.fn_my_permissions("
    "QUOTENAME(SCHEMA_NAME(o.schema_id)) + '.' + QUOTENAME(o.name), 'OBJECT') p WHERE o.is_ms_shipped = 0 "
    "AND o.type IN ('U', 'V', 'P', 'PC', 'FN', 'FS', 'FT', 'IF', 'TF', 'AF', 'X', 'SN', 'SO', 'SQ') "
    f"AND p.permission_name NOT IN ({OBJECT_READ})) "
    "AND NOT EXISTS (SELECT 1 FROM sys.database_permissions g WHERE g.state IN ('G', 'W') "
    "AND g.grantee_principal_id <> 0 AND (g.grantee_principal_id = DATABASE_PRINCIPAL_ID() "
    "OR IS_MEMBER(USER_NAME(g.grantee_principal_id)) = 1) "
    f"AND g.permission_name NOT IN ({DATABASE_READ})) "
    "AND NOT EXISTS (SELECT 1 FROM sys.server_permissions g WHERE g.state IN ('G', 'W') "
    "AND (g.grantee_principal_id = SUSER_ID() OR (SUSER_NAME(g.grantee_principal_id) <> 'public' "
    "AND IS_SRVROLEMEMBER(SUSER_NAME(g.grantee_principal_id)) = 1)) "
    f"AND g.permission_name NOT IN ({SERVER_READ})) "
    "THEN 0 ELSE 1 END"
)
VERSION = "SELECT CAST(SERVERPROPERTY('ProductVersion') AS varchar(32))"
KINDS = {"U": "table", "V": "view"}


# Errors of the query itself: syntax, a name, a conversion, an overflow, a division by zero, a function used wrongly.
QUERY_ERRORS = frozenset({102, 137, 156, 174, 195, 207, 208, 241, 242, 245, 4104, 4121, 8114, 8115, 8120, 8134, 8152})


def classify(error):
    """A fixed code for what went wrong. The text and the number are looked at here and nowhere else."""
    name = type(error).__name__.lower()
    number = getattr(error, "number", None)
    text = str(error).lower()
    if "cannot open database" in text or number == 4060:
        return "database_not_found"
    if number in (18456, 18452, 18451, 18470) or "login failed" in text:
        return "auth_failed"
    if number in (229, 230, 262, 297, 300, 916):
        return "permission_denied"
    if number in QUERY_ERRORS or type(error).__name__ in (
        "ProgrammingError",
        "DataError",
        "IntegrityError",
        "NotSupportedError",
    ):
        return "query_error"  # the query itself: a name, a value, a type, a syntax the server does not take
    if isinstance(error, TimeoutError) or "timeout" in name or "timed out" in text:
        return "timeout"
    if "ssl" in name or "ssl" in text or "certificate" in text or "tls" in text or "openssl" in type(error).__module__:
        return "tls_failed"
    if isinstance(error, (ConnectionError, socket.gaierror, OSError)):
        return "unreachable"
    return "failed"


def _driver():
    try:
        import pytds
    except ImportError:
        raise DriverError("driver_missing") from None
    # python-tds logs every failed attempt WITH the server's error text (user name, "Login failed for user ..."): that
    # text
    # must not reach the application's log, so its loggers are silenced and cut off from the root logger.
    logger = logging.getLogger("pytds")
    logger.setLevel(logging.CRITICAL + 1)
    logger.propagate = False
    return pytds


@contextlib.contextmanager
def _connection(target, password, timeout, query_timeout=None, autocommit=True):
    pytds = _driver()
    with contextlib.ExitStack() as stack:
        options = {}
        if target.ssl_mode in ("verify-ca", "verify-full"):
            if not target.ca_pem:
                raise DriverError("tls_failed")
            folder = stack.enter_context(tempfile.TemporaryDirectory())
            path = Path(folder) / "ca.pem"
            path.write_text(target.ca_pem, encoding="utf-8")
            options = {"cafile": str(path), "validate_host": target.ssl_mode == "verify-full"}
        sock, failure = None, None
        for address in target.addresses:  # the judged addresses, in order; nothing else is connected to
            try:
                sock = socket.create_connection(
                    (address, target.port), timeout=max(1.0, timeout / len(target.addresses))
                )
                break
            except OSError as error:
                failure = error
        if sock is None:
            raise DriverError(classify(failure)) from None
        stack.callback(sock.close)
        try:
            conn = pytds.connect(
                dsn=target.host,
                port=target.port,
                sock=sock,
                database=target.database,
                user=target.user,
                password=password,
                login_timeout=timeout,
                timeout=query_timeout or timeout,
                appname="rag-connector",
                autocommit=autocommit,
                readonly=True,
                disable_connect_retry=True,
                **options,
            )
        except Exception as error:  # noqa: BLE001 - every driver failure becomes one fixed code
            raise DriverError(classify(error)) from None
        stack.callback(conn.close)
        conn.rag_socket = sock  # (so that a run can be stopped by closing it)
        yield conn


def _scalar(conn, sql):
    with conn.cursor() as cursor:
        cursor.execute(sql)
        row = cursor.fetchone()
    return row[0] if row else None


def _can_write(conn):
    """Anything but a clear "no" counts as "yes"."""
    return _scalar(conn, CAN_CHANGE_ANYTHING) != 0


def test(target, password, timeout):
    try:
        with _connection(target, password, timeout) as conn:
            _scalar(conn, "SELECT 1")
            version = str(_scalar(conn, VERSION) or "")
            writable = _can_write(conn)
    except DriverError:
        raise
    except Exception as error:  # noqa: BLE001
        raise DriverError(classify(error)) from None
    return TestResult(version=".".join(version.split(".")[:2]), can_write=writable)


def run(target, password, sql, limits, cancel=None):
    """Run ONE query that the validator (sqlcheck.py) wrote and return its bounded result. This module does not look at
    the SQL: it is never called with a text that did not come from there.

    SQL Server has no read-only session, so the protection is the account: it must hold NO permission that could change
    anything (CAN_CHANGE_ANYTHING, checked on this very connection, every time; otherwise `account_can_write` and
    nothing is run). The transaction that is always rolled back is only a second net: it does not undo everything (a
    sequence value, something that leaves the server). Time: the socket timeout bounds every wait, a timer closes the
    socket when the whole run has taken too long, and so does a client that goes away. Rows are read one at a time;
    reading stops at the row or byte limit and the connection is dropped."""
    started = time.monotonic()
    shown = None
    expired = threading.Event()
    try:
        with _connection(target, password, limits.connect_timeout_s, limits.timeout_s, autocommit=False) as conn:

            def stop():
                conn.rag_socket.shutdown(socket.SHUT_RDWR)

            def expire():
                expired.set()
                with contextlib.suppress(OSError):
                    stop()

            timer = threading.Timer(limits.timeout_s, expire)
            timer.daemon = True
            if cancel:
                cancel.attach(stop)
            try:
                if cancel and cancel.cancelled:
                    raise DriverError("cancelled")
                timer.start()
                with conn.cursor() as cursor:
                    cursor.execute("SET LOCK_TIMEOUT 5000")
                if _can_write(conn):
                    raise DriverError("account_can_write")
                with conn.cursor() as cursor:
                    cursor.execute(sql)
                    shown = [{"name": str(c[0] or ""), "type": ""} for c in (cursor.description or [])]
                    rows, truncated, size = [], None, 0
                    while True:
                        raw = cursor.fetchone()
                        if expired.is_set() or time.monotonic() - started > limits.timeout_s:
                            raise DriverError("timeout")
                        if raw is None:
                            break
                        if len(rows) >= limits.max_rows:
                            truncated = "rows"
                            break
                        row = [cell(v, limits.max_cell_chars) for v in raw]
                        size += row_size(row)
                        if size > limits.max_bytes:
                            truncated = "bytes"
                            break
                        rows.append(row)
                if truncated:  # unread rows are still on the wire: drop the connection instead of waiting for them
                    conn.rag_socket.shutdown(socket.SHUT_RDWR)
                else:
                    conn.rollback()
            finally:
                timer.cancel()
                if cancel:
                    cancel.detach()
    except Exception as error:  # noqa: BLE001
        if cancel and cancel.cancelled:
            raise DriverError("cancelled") from None
        if expired.is_set():  # (the timer closed the socket: whatever the driver made of that, it was the time)
            raise DriverError("timeout") from None
        if isinstance(error, DriverError):
            raise
        raise DriverError(classify(error)) from None
    return QueryResult(shown, rows, truncated, round(time.monotonic() - started, 3))


def _rows(conn, sql, deadline):
    """The rows of a catalog query, a batch at a time; stops with `timeout` when the deadline has passed."""
    with conn.cursor() as cursor:
        cursor.execute(sql)
        while True:
            batch = cursor.fetchmany(FETCH)
            if not batch:
                return
            if time.monotonic() > deadline:
                raise DriverError("timeout")
            yield from batch


def _grouped(rows, key_size):
    """Consecutive rows with the same first `key_size` values, as (key, [the rest of each row]): one key at a time in
    memory (the queries are ordered so that a key's rows are together)."""
    current, items = None, []
    for row in rows:
        key = tuple(row[:key_size])
        if key != current:
            if current is not None:
                yield current, items
            current, items = key, []
        items.append(row[key_size:])
    if current is not None:
        yield current, items


def introspect(target, password, sink, timeout):
    deadline = time.monotonic() + timeout
    try:
        with _connection(target, password, min(timeout, 15)) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SET LOCK_TIMEOUT 5000")
            for schema, name, kind in _rows(conn, OBJECTS, deadline):
                sink.object(schema, name, KINDS[kind.strip()])
            for schema, name, ordinal, column, data_type, nullable in _rows(conn, COLUMNS, deadline):
                sink.column(schema, name, ordinal, column, data_type, bool(nullable))
            for (schema, name, primary, constraint), items in _grouped(_rows(conn, KEYS, deadline), 4):
                sink.constraint(
                    schema, name, "primary_key" if primary else "unique", constraint, [item[0] for item in items]
                )
            for (schema, name, constraint), items in _grouped(_rows(conn, FOREIGN_KEYS, deadline), 3):
                sink.constraint(
                    schema,
                    name,
                    "foreign_key",
                    constraint,
                    [i[0] for i in items],
                    items[0][1],
                    items[0][2],
                    [i[3] for i in items],
                )
            for (schema, name, unique, primary, index), items in _grouped(_rows(conn, INDEXES, deadline), 5):
                sink.index(schema, name, index, [i[0] for i in items], bool(unique), bool(primary))
    except DriverError:
        raise
    except Exception as error:  # noqa: BLE001
        raise DriverError(classify(error)) from None
    return Introspection()
