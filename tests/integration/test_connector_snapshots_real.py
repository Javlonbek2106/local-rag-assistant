"""M4.2 against REAL external servers in Docker (PostgreSQL 15 and 17, SQL Server 2022): the structure of a database is
changed with real DDL between discoveries, and the snapshots, fingerprints and changes are what the rules say.

Needs RAG_INTEGRATION=1, RAG_TEST_DATABASE_URL and Docker. Every test puts the external schema back as it found it.
"""

import pytest

pytest.importorskip("cryptography")
pytest.importorskip("pytds")

from rag import config  # noqa: E402
from rag.connector.service import Service  # noqa: E402
from tests import stack as stack_tools  # noqa: E402
from tests.auth_support import Events  # noqa: E402
from tests.connector_support import KeyRing  # noqa: E402
from tests.db.test_connector_service import admin  # noqa: E402
from tests.integration import external_servers as ext  # noqa: E402
from tests.integration.test_connector_databases import mssql_payload, pg_payload  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.db]


@pytest.fixture(autouse=True)
def clean(database):
    with admin() as conn:
        conn.execute("DELETE FROM connections")
    yield
    with admin() as conn:
        conn.execute("DELETE FROM connections")


@pytest.fixture(scope="module")
def docker_ready():
    if not stack_tools.docker_available():
        pytest.skip("Docker is not available")


@pytest.fixture(scope="module")
def pg15(docker_ready):
    server = ext.Postgres("postgres:15-alpine").start()
    yield server
    server.stop()


@pytest.fixture(scope="module")
def pg17(docker_ready):
    server = ext.Postgres("postgres:17-alpine").start()
    yield server
    server.stop()


@pytest.fixture(scope="module")
def mssql_server(docker_ready):
    server = ext.SqlServer().start()
    yield server
    server.stop()


class Backend:
    """One external server and the DDL that changes it in the ways the tests need (and puts it back)."""

    def __init__(self, server, payload, run, ddl):
        self.server, self.payload, self.run, self.ddl = server, payload, run, ddl


PG_DDL = {
    "table": ("CREATE TABLE sales.zz_added (id int PRIMARY KEY, note text);", "DROP TABLE sales.zz_added;"),
    "column": ("ALTER TABLE sales.customer ADD COLUMN email text;", "ALTER TABLE sales.customer DROP COLUMN email;"),
    "type": (
        "ALTER TABLE sales.customer ALTER COLUMN name TYPE varchar(80);",
        "ALTER TABLE sales.customer ALTER COLUMN name TYPE text;",
    ),
    "nullable": (
        "ALTER TABLE sales.order_line ALTER COLUMN qty DROP NOT NULL;",
        "ALTER TABLE sales.order_line ALTER COLUMN qty SET NOT NULL;",
    ),
    "fk": (
        "ALTER TABLE sales.order_line DROP CONSTRAINT order_line_customer_id_fkey;",
        "ALTER TABLE sales.order_line ADD CONSTRAINT order_line_customer_id_fkey FOREIGN KEY (customer_id) "
        "REFERENCES sales.customer(id);",
    ),
    "index": ("CREATE INDEX ix_zz ON sales.customer (name);", "DROP INDEX sales.ix_zz;"),
}
MSSQL_DDL = {
    "table": (
        "CREATE TABLE sales.zz_added (id INT NOT NULL PRIMARY KEY, note NVARCHAR(50));",
        "DROP TABLE sales.zz_added;",
    ),
    "column": (
        "ALTER TABLE sales.customer ADD email NVARCHAR(100) NULL;",
        "ALTER TABLE sales.customer DROP COLUMN email;",
    ),
    "type": (
        "ALTER TABLE sales.customer ALTER COLUMN name NVARCHAR(80) NOT NULL;",
        "ALTER TABLE sales.customer ALTER COLUMN name NVARCHAR(100) NOT NULL;",
    ),
    "nullable": (
        "ALTER TABLE sales.order_line ALTER COLUMN qty INT NULL;",
        "ALTER TABLE sales.order_line ALTER COLUMN qty INT NOT NULL;",
    ),
    "fk": (
        "ALTER TABLE sales.order_line DROP CONSTRAINT fk_ol_customer;",
        "ALTER TABLE sales.order_line ADD CONSTRAINT fk_ol_customer FOREIGN KEY (customer_id) REFERENCES "
        "sales.customer(id);",
    ),
    "index": ("CREATE INDEX ix_zz ON sales.customer (name);", "DROP INDEX ix_zz ON sales.customer;"),
}


@pytest.fixture(params=["pg15", "pg17", "mssql"])
def backend(request):
    if request.param == "mssql":
        server = request.getfixturevalue("mssql_server")
        return Backend(
            server,
            lambda **c: mssql_payload(server, **c),
            lambda sql: server.script(f"USE rag_ext;\nGO\n{sql}\nGO\n"),
            MSSQL_DDL,
        )
    server = request.getfixturevalue(request.param)
    return Backend(server, lambda **c: pg_payload(server, **c), server.sql, PG_DDL)


@pytest.fixture
def keys():
    return KeyRing()


@pytest.fixture
def events():
    return Events()


@pytest.fixture
def service(keys, events):
    return Service(keys.vault, events)


@pytest.fixture
def owner():
    from rag.auth.principals import Principals

    return Principals().local_owner()["id"]


@pytest.fixture
def connection(backend, service, owner):
    made = service.create(owner, backend.payload())
    return made["id"]


class Altered:
    """`with Altered(backend, "column"):` — the DDL is in effect inside, and undone afterwards whatever happens."""

    def __init__(self, backend, *names):
        self.backend, self.names = backend, names

    def __enter__(self):
        for name in self.names:
            self.backend.run(self.backend.ddl[name][0])
        return self

    def __exit__(self, *exc):
        for name in reversed(self.names):
            self.backend.run(self.backend.ddl[name][1])


def changes(service, owner, connection_id, version):
    listed = service.changes(owner, connection_id, version=version, limit=500)["changes"]
    return {(c["object_type"], c["schema_name"], c["object_name"], c["sub_name"], c["change_type"]) for c in listed}


# ---------- the first and the unchanged discovery ----------


def test_a_first_discovery_is_snapshot_1_and_an_unchanged_one_adds_nothing(service, owner, connection):
    first = service.discover(owner, connection)["snapshot"]
    assert first["version"] == 1 and first["first"] is True and len(first["fingerprint"]) == 64
    for _ in range(2):
        again = service.discover(owner, connection)["snapshot"]
        assert again["changed"] is False and again["version"] == 1 and again["fingerprint"] == first["fingerprint"]
    assert len(service.snapshots(owner, connection)) == 1 and service.changes(owner, connection)["total"] == 0
    assert service.freshness(owner, connection)["state"] == "current"


def test_the_external_database_is_not_changed_by_any_of_it(backend, service, owner, connection):
    before = backend.server.checksum()
    service.discover(owner, connection)
    with Altered(backend, "column"):
        service.discover(owner, connection)
    assert backend.server.checksum() == before


# ---------- each kind of real change ----------


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("table", {("table", "sales", "zz_added", "", "added")}),
        ("column", {("column", "sales", "customer", "email", "added")}),
        ("type", {("column", "sales", "customer", "name", "modified")}),
        ("nullable", {("column", "sales", "order_line", "qty", "modified")}),
    ],
)
def test_a_real_change_is_found_and_putting_it_back_returns_to_the_first_fingerprint(
    backend, service, owner, connection, kind, expected
):
    first = service.discover(owner, connection)["snapshot"]
    with Altered(backend, kind):
        second = service.discover(owner, connection)["snapshot"]
        assert second["changed"] is True and second["version"] == 2 and second["fingerprint"] != first["fingerprint"]
        assert changes(service, owner, connection, 2) == expected
    back = service.discover(owner, connection)["snapshot"]
    assert back["version"] == 3 and back["fingerprint"] == first["fingerprint"]  # the same structure, the same bytes
    inverse = {"added": "removed", "removed": "added"}
    undone = changes(service, owner, connection, 3)
    assert undone == {(o, s, n, sub, inverse.get(c, c)) for o, s, n, sub, c in expected}


def test_a_dropped_and_restored_foreign_key_is_a_relationship_change_both_ways(backend, service, owner, connection):
    service.discover(owner, connection)
    with Altered(backend, "fk"):
        service.discover(owner, connection)
        gone = changes(service, owner, connection, 2)
        assert len(gone) == 1 and next(iter(gone))[0] == "relationship" and next(iter(gone))[4] == "removed"
        assert {(r["object_name"], r["ref_object"]) for r in service.relationships(owner, connection)} == {
            ("order_note", "order_line")
        }
    service.discover(owner, connection)
    assert next(iter(changes(service, owner, connection, 3)))[4] == "added"


def test_a_new_index_is_a_change_and_a_composite_primary_key_is_part_of_the_fingerprint(
    backend, service, owner, connection
):
    service.discover(owner, connection)
    with Altered(backend, "index"):
        service.discover(owner, connection)
        assert changes(service, owner, connection, 2) == {("index", "sales", "customer", "ix_zz", "added")}
    detail = service.object_detail(owner, connection, "sales", "order_line")
    keys = [c for c in detail["constraints"] if c["kind"] == "primary_key"]
    assert keys[0]["columns"] == ["order_id", "line_no"]


# ---------- a discovery that fails ----------


def test_a_discovery_that_fails_keeps_the_last_good_snapshot_and_a_good_one_clears_it(
    backend, service, owner, connection
):
    service.discover(owner, connection)
    good = service.snapshots(owner, connection)
    service.update(owner, connection, {"password": "definitely-not-the-password-1"})
    with pytest.raises(Exception) as failed:  # noqa: B017
        service.discover(owner, connection)
    assert getattr(failed.value, "code", "") == "discovery_failed"
    assert (
        service.snapshots(owner, connection) == good
        and service.objects(owner, connection)[1] == len(good) * good[0]["objects_count"]
    )
    assert service.freshness(owner, connection)["state"] == "failed"
    service.update(owner, connection, {"password": ext.READER_PASSWORD})
    service.discover(owner, connection)
    assert service.freshness(owner, connection)["state"] == "current" and len(service.snapshots(owner, connection)) == 1


def test_a_discovery_cut_off_by_the_time_limit_leaves_no_partial_snapshot(
    backend, service, owner, connection, monkeypatch
):
    service.discover(owner, connection)
    with Altered(backend, "table"):
        monkeypatch.setattr(config, "CONNECTOR_DISCOVERY_TIMEOUT_S", 0.001)
        with pytest.raises(Exception):  # noqa: B017
            service.discover(owner, connection)
        monkeypatch.undo()
        assert len(service.snapshots(owner, connection)) == 1
        assert service.discover(owner, connection)["snapshot"]["version"] == 2  # and a later one works


# ---------- the context and the selection on a real schema ----------


def test_the_context_of_a_real_schema_has_only_the_selection_and_no_row(backend, service, owner, connection):
    service.discover(owner, connection)
    service.put_selections(
        owner,
        connection,
        [
            {"kind": "table", "schema": "sales", "object": "customer"},
            {"kind": "column", "schema": "sales", "object": "customer", "column": "secret_note", "mode": "exclude"},
            {"kind": "table", "schema": "sales", "object": "order_line"},
        ],
    )
    context = service.context(owner, connection)
    text = str(context)
    assert context["usable"] is True and ext.SECRET_ROW not in text and "Hidden Customer Name" not in text
    assert "secret_note" not in text and "balance" in text
    objects = {(s["name"], o["name"]) for s in context["context"]["schemas"] for o in s["objects"]}
    assert objects == {("sales", "customer"), ("sales", "order_line")}
    order_line = next(o for s in context["context"]["schemas"] for o in s["objects"] if o["name"] == "order_line")
    assert (
        order_line["primary_key"] == ["order_id", "line_no"]
        and order_line["foreign_keys"][0]["references"]["object"] == "customer"
    )
    assert service.context(owner, connection)["budget"] == context["budget"]  # the same bytes again
