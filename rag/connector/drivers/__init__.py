"""Adapters for the external databases. One module per provider, found by name (a plain dict-like lookup, no framework).

A driver module has two functions:
    test(target, password, timeout) -> TestResult            connect, authenticate, SELECT 1, read-only checks
    introspect(target, password, sink, timeout)              stream the catalog (not the data) into `sink`
and raises DriverError(code) for everything that can go wrong: a driver's own message is never passed on.

Drivers import neither rag.db nor rag.auth. What they find goes to a `sink` object they are given
(repository.SchemaWriter). The third-party libraries are imported only when a driver is used.
"""

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
