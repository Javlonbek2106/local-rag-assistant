"""Adapters for the external databases. One module per provider, found by name (a plain dict-like lookup, no framework).

A driver module has two functions:
    test(target, password, timeout) -> TestResult            connect, authenticate, SELECT 1, read-only checks
    introspect(target, password, sink, timeout)              stream the catalog (not the data) into `sink`
and raises DriverError(code) for everything that can go wrong: a driver's own message is never passed on.

Drivers import neither rag.db nor rag.auth. What they find goes to a `sink` object they are given
(repository.SchemaWriter). The third-party libraries are imported only when a driver is used.
"""

import datetime
import decimal
import json
import math
import threading
from typing import NamedTuple

from rag.connector.errors import ConnectorError


class Target(NamedTuple):
    host: str  # the name, for the certificate check
    addresses: tuple  # the judged addresses; they are tried in this order, nothing else is ever connected to
    port: int
    database: str
    user: str
    ssl_mode: str  # disable | require | verify-ca | verify-full
    ca_pem: str | None = None


class TestResult(NamedTuple):
    __test__ = False  # (not a pytest class)
    version: str
    can_write: bool | None  # None: could not be told


class Introspection(NamedTuple):
    pass


def driver(provider):
    if provider == "postgresql":
        from rag.connector.drivers import postgres

        return postgres
    if provider == "mssql":
        from rag.connector.drivers import mssql

        return mssql
    raise ConnectorError("invalid_provider")


# ---------- running a query (M4.4) ----------


class RunLimits(NamedTuple):
    timeout_s: float  # the whole query, from the first statement to the last row
    connect_timeout_s: float
    max_rows: int
    max_bytes: int  # the size of the returned values, as JSON text
    max_cell_chars: int


class QueryResult(NamedTuple):
    columns: list  # [{"name", "type"}]
    rows: list  # lists of JSON-safe values
    truncated: str | None  # None, "rows" (more rows than the limit) or "bytes" (more data than the limit)
    seconds: float


class CancelToken:
    """Lets another thread (the one that notices the client has gone) stop a query that is running. The driver attaches
    the function that really stops it (the connection's cancel request, or closing its socket)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._action = None
        self.cancelled = False

    def attach(self, action):
        with self._lock:
            self._action = action
            fire = self.cancelled
        if fire:
            self._fire(action)

    def detach(self):
        with self._lock:
            self._action = None

    def cancel(self):
        with self._lock:
            self.cancelled = True
            action = self._action
        if action:
            self._fire(action)

    @staticmethod
    def _fire(action):
        try:
            action()
        except Exception:  # noqa: BLE001 - the query may already be over; there is nothing more to stop
            pass


def cell(value, max_chars):
    """One value of a result as something JSON can carry, and not longer than `max_chars`. The text of a value is cut;
    binary values are shown as the start of their hex; nothing is ever passed on as an object."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if abs(value) < 2**53 else str(value)  # a browser cannot hold more in a number
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
        return "0x" + data[:32].hex() + ("…" if len(data) > 32 else "")
    if isinstance(value, (list, tuple)):
        return [cell(item, max_chars) for item in value[:200]]
    if isinstance(value, dict):
        text = json.dumps(value, default=str, ensure_ascii=False)
    else:
        text = value if isinstance(value, str) else str(value)
    return text if len(text) <= max_chars else text[:max_chars] + "…"


def row_size(row):
    """What a row costs against the byte limit: its length as JSON text."""
    return len(json.dumps(row, ensure_ascii=False, default=str))
