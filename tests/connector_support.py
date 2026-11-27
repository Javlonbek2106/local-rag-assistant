"""Stand-ins for the connector tests: a fake driver, a fixed resolver, a key ring, a second principal."""

import socket

from rag.connector.drivers import Introspection, QueryResult, TestResult
from rag.connector.errors import DriverError
from rag.connector.vault import Vault, new_key_line

PUBLIC_ADDRESS = "93.184.216.34"
PASSWORD = "Sentinel-Pa55word!-zq81"  # a value that must be found NOWHERE it should not be
PAYLOAD = {
    "name": "Sales",
    "description": "the sales database",
    "provider": "postgresql",
    "host": "db.example.org",
    "port": 5432,
    "database_name": "sales",
    "username": "reader",
    "password": PASSWORD,
    "ssl_mode": "verify-full",
    "options": {},
}


def resolver(*addresses):
    answers = addresses or (PUBLIC_ADDRESS,)

    def resolve(host, port, family=0, kind=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in answers]

    return resolve


def internal():
    return "10.255.255.1", 5433, "rag_internal"  # nowhere near what the tests connect to


class KeyRing:
    """The key ring a test controls: `text` can be changed or removed like the file it stands for."""

    def __init__(self, *key_ids):
        self.text = "\n".join(new_key_line(k) for k in (key_ids or ("k1",))) + "\n"
        self.missing = False

    def load(self):
        if self.missing:
            error = RuntimeError("the key file is gone")
            error.code = "missing"
            raise error
        return self.text

    @property
    def vault(self):
        return Vault(self.load)


# A small schema with every shape discovery must get right: two schemas, a composite primary key, a composite foreign
# key,
# a table without a primary key, a view, a unique constraint and an index.
SAMPLE = {
    "objects": [
        ("hr", "employee", "table"),
        ("hr", "no_pk", "table"),
        ("sales", "big_orders", "view"),
        ("sales", "customer", "table"),
        ("sales", "order_line", "table"),
        ("sales", "order_note", "table"),
    ],
    "columns": [
        ("hr", "employee", 1, "id", "integer", False),
        ("hr", "employee", 2, "email", "text", False),
        ("hr", "no_pk", 1, "a", "integer", True),
        ("sales", "big_orders", 1, "order_id", "integer", False),
        ("sales", "customer", 1, "id", "integer", False),
        ("sales", "customer", 2, "name", "text", False),
        ("sales", "order_line", 1, "order_id", "integer", False),
        ("sales", "order_line", 2, "line_no", "integer", False),
        ("sales", "order_line", 3, "customer_id", "integer", False),
        ("sales", "order_note", 1, "order_id", "integer", False),
        ("sales", "order_note", 2, "line_no", "integer", False),
    ],
    "constraints": [
        ("hr", "employee", "primary_key", "pk_emp", ["id"]),
        ("hr", "employee", "unique", "uq_email", ["email"]),
        ("sales", "customer", "primary_key", "pk_customer", ["id"]),
        ("sales", "order_line", "primary_key", "pk_ol", ["order_id", "line_no"]),
        ("sales", "order_line", "foreign_key", "fk_ol_customer", ["customer_id"], "sales", "customer", ["id"]),
        (
            "sales",
            "order_note",
            "foreign_key",
            "fk_note_line",
            ["order_id", "line_no"],
            "sales",
            "order_line",
            ["order_id", "line_no"],
        ),
    ],
    "indexes": [
        ("hr", "employee", "pk_emp", ["id"], True, True),
        ("hr", "employee", "ix_email", ["email"], False, False),
    ],
}


class FakeDriver:
    """A driver module that does what a test says: records what it was given, answers, or fails with a code."""

    __test__ = False

    def __init__(self):
        self.calls, self.test_error, self.discover_error, self.version = [], None, None, "17.2"
        self.can_write, self.sample, self.fail_after = False, SAMPLE, None
        self.run_result, self.run_error, self.run_gate = (
            None,
            None,
            None,
        )  # M4.4: what run() answers, fails with, waits for

    def test(self, target, password, timeout):
        self.calls.append(("test", target, password, timeout))
        if self.test_error:
            raise self.test_error
        return TestResult(version=self.version, can_write=self.can_write)

    def introspect(self, target, password, sink, timeout):
        self.calls.append(("introspect", target, password, timeout))
        for number, row in enumerate(self.sample["objects"]):
            if self.fail_after is not None and number >= self.fail_after:
                raise DriverError(self.discover_error or "timeout")
            sink.object(*row)
        for row in self.sample["columns"]:
            sink.column(*row)
        for row in self.sample["constraints"]:
            sink.constraint(*row)
        for row in self.sample["indexes"]:
            sink.index(*row)
        if self.discover_error:
            raise DriverError(self.discover_error)
        return Introspection()

    def run(self, target, password, sql, limits, cancel=None):
        self.calls.append(("run", target, password, sql, limits))
        if cancel is not None:
            cancel.attach(lambda: None)
        try:
            if self.run_gate is not None:
                self.run_gate.wait(10)
            if self.run_error:
                raise self.run_error
            return self.run_result or QueryResult([{"name": "id", "type": "int"}], [[1], [2]], None, 0.01)
        finally:
            if cancel is not None:
                cancel.detach()

    def __call__(self, provider):
        self.calls.append(("lookup", provider))
        return self
