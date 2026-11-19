"""M4.1b against REAL external servers in Docker: PostgreSQL 15 and 17 (plain and with TLS) and SQL Server 2022 (plain
and with TLS). The real drivers, the real vault, the real internal database (a throw-away schema).

Needs RAG_INTEGRATION=1, RAG_TEST_DATABASE_URL and Docker (the SQL Server image is about 2 GB). The SQL Server TLS
tests restart the one container, so they come last in this file.
"""

import json
import socket
import threading

import pytest

pytest.importorskip("cryptography")
pytest.importorskip("pytds")

from rag import config  # noqa: E402
from rag.connector import repository  # noqa: E402
from rag.connector.drivers import Target, mssql, postgres  # noqa: E402
from rag.connector.errors import ConnectorError, DriverError  # noqa: E402
from rag.connector.service import Service  # noqa: E402
from tests import stack as stack_tools  # noqa: E402
from tests.auth_support import Events  # noqa: E402
from tests.connector_support import KeyRing  # noqa: E402
from tests.db.test_connector_service import admin  # noqa: E402
from tests.integration import external_servers as ext  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.db]


@pytest.fixture(autouse=True)
def clean(database):
    with admin() as conn:
        conn.execute("DELETE FROM connections")
        conn.execute("DELETE FROM principals WHERE kind <> 'local-owner'")
    yield
    with admin() as conn:
        conn.execute("DELETE FROM connections")
        conn.execute("DELETE FROM principals WHERE kind <> 'local-owner'")


@pytest.fixture(scope="module")
def docker_ready():
    if not stack_tools.docker_available():
        pytest.skip("Docker is not available")


@pytest.fixture(params=["postgres:15-alpine", "postgres:17-alpine"], scope="module")
def pg(request, docker_ready):
    server = ext.Postgres(request.param).start()
    yield server
    server.stop()


@pytest.fixture(scope="module")
def certificates():
    return ext.make_certificates()


@pytest.fixture(scope="module")
def pg_tls(docker_ready, certificates):
    _ca, certificate, key = certificates
    server = ext.Postgres("postgres:17-alpine", tls=(certificate, key)).start()
    yield server
    server.stop()


@pytest.fixture(scope="module")
def mssql_server(docker_ready):
    server = ext.SqlServer().start()
    yield server
    server.stop()


@pytest.fixture(scope="module")
def mssql_tls(mssql_server, certificates):
    """The SQL Server container, from now on requiring encryption (the plain tests above must have run: file order)."""
    _ca, certificate, key = certificates
    mssql_server.force_tls(certificate, key)
    return mssql_server


@pytest.fixture
def keys():
    return KeyRing()


@pytest.fixture
def events():
    return Events()


@pytest.fixture
def service(keys, events):
    return Service(keys.vault, events)  # the real drivers, the real resolver, the real "internal database" rule


@pytest.fixture
def owner():
    from rag.auth.principals import Principals

    return Principals().local_owner()["id"]


def pg_payload(server, **changes):
    return {
        "name": "ext",
        "provider": "postgresql",
        "host": "127.0.0.1",
        "port": server.port,
        "database_name": "ext",
        "username": "reader",
        "password": ext.READER_PASSWORD,
        "ssl_mode": "disable",
        **changes,
    }


def mssql_payload(server, **changes):
    return {
        "name": "mssql",
        "provider": "mssql",
        "host": "127.0.0.1",
        "port": server.port,
        "database_name": "rag_ext",
        "username": "reader",
        "password": ext.READER_PASSWORD,
        "ssl_mode": "disable",
        **changes,
    }


def outcome(service, owner, payload):
    made = service.create(owner, payload)
    return made, service.test(owner, made["id"])


def tree(service, owner, connection_id):
    """{schema.name: (type, [columns], {kind: [constraint columns]})} of a discovered connection."""
    out = {}
    rows, _ = service.objects(owner, connection_id, limit=500)
    for row in rows:
        detail = service.object_detail(owner, connection_id, row["schema_name"], row["object_name"])
        kinds = {}
        for c in detail["constraints"]:
            kinds.setdefault(c["kind"], []).append(c["columns"])
        out[f"{row['schema_name']}.{row['object_name']}"] = (
            detail["type"],
            [c["column_name"] for c in detail["columns"]],
            kinds,
            detail,
        )
    return out


# ---------- PostgreSQL: connection tests ----------


def test_postgres_a_good_connection_is_tested_and_the_account_is_found_read_only(pg, service, owner):
    made, result = outcome(service, owner, pg_payload(pg))
    assert result["status"] == "ok" and result["error"] is None and result["warning"] is None
    assert result["server_version"].startswith(pg.image.split(":")[1].split("-")[0] + ".")  # 15.x, 17.x
    stored = service.get(owner, made["id"])
    assert stored["last_test_status"] == "ok" and stored["last_tested_at"] is not None


def test_postgres_an_account_that_can_write_gets_a_warning(pg, service, owner):
    _, result = outcome(service, owner, pg_payload(pg, username="writer", password=ext.WRITER_PASSWORD, name="w"))
    assert result["status"] == "ok" and result["warning"] == "account_can_write"
    _, admin_result = outcome(
        service, owner, pg_payload(pg, username="postgres", password=ext.ADMIN_PASSWORD, name="a")
    )
    assert admin_result["warning"] == "account_can_write"


@pytest.mark.parametrize(
    "change,code",
    [
        ({"password": "not-the-password-0000"}, "auth_failed"),
        ({"username": "nobody"}, "auth_failed"),
        ({"database_name": "nodb"}, "database_not_found"),
    ],
)
def test_postgres_failures_are_fixed_codes_and_say_nothing_else(pg, service, owner, caplog, change, code):
    caplog.set_level("DEBUG")
    made, result = outcome(service, owner, pg_payload(pg, **change))
    assert result["status"] == "failed" and result["error"] == code
    assert service.get(owner, made["id"])["last_test_error"] == code
    shown = json.dumps([result, service.get(owner, made["id"])], default=str) + caplog.text
    assert "not-the-password-0000" not in shown and ext.READER_PASSWORD not in shown and "FATAL" not in shown


def test_postgres_a_closed_port_is_unreachable(service, owner):
    free = stack_tools.free_port()
    made, result = outcome(
        service,
        owner,
        {
            "name": "closed",
            "provider": "postgresql",
            "host": "127.0.0.1",
            "port": free,
            "database_name": "x",
            "username": "u",
            "password": "pw-123456",
            "ssl_mode": "disable",
            "options": {"connect_timeout_s": 2},
        },
    )
    assert result["error"] in ("unreachable", "timeout")  # refused at once on Linux; Windows may wait out the timeout


def test_postgres_a_server_that_never_answers_ends_in_a_timeout_within_the_bound(service, owner):
    import time

    quiet = socket.socket()
    quiet.bind(("127.0.0.1", 0))
    quiet.listen(5)  # accepts the connection at the operating-system level and says nothing, ever
    try:
        port = quiet.getsockname()[1]
        started = time.monotonic()
        made, result = outcome(
            service,
            owner,
            {
                "name": "quiet",
                "provider": "postgresql",
                "host": "127.0.0.1",
                "port": port,
                "database_name": "x",
                "username": "u",
                "password": "pw-123456",
                "ssl_mode": "disable",
                "options": {"connect_timeout_s": 2},
            },
        )
        assert result["error"] == "timeout" and time.monotonic() - started < 8
    finally:
        quiet.close()


# ---------- PostgreSQL: discovery ----------


def test_postgres_discovery_finds_schemas_tables_views_columns_and_keys(pg, service, owner):
    made = service.create(owner, pg_payload(pg))
    found = service.discover(owner, made["id"])
    assert found["objects"] == 8 and found["schema_discovered_at"] is not None
    shape = tree(service, owner, made["id"])
    assert set(shape) == {
        "hr.employee",
        "hr.no_pk",
        "sales.big_orders",
        "sales.customer",
        "sales.events",
        "sales.order_line",
        "sales.order_note",
        "sales.order_totals",
    }  # partitions are not listed
    assert shape["sales.big_orders"][0] == "view" and shape["sales.order_totals"][0] == "materialized_view"
    assert shape["sales.customer"][1] == ["id", "name", "secret_note", "balance"]
    assert shape["sales.order_line"][2]["primary_key"] == [["order_id", "line_no"]]  # composite primary key
    assert shape["sales.order_note"][2]["foreign_key"] == [["order_id", "line_no"]]  # composite foreign key
    fk = shape["sales.order_note"][3]["constraints"][0]
    assert (fk["ref_schema"], fk["ref_object"], fk["ref_columns"]) == ("sales", "order_line", ["order_id", "line_no"])
    assert shape["hr.employee"][2]["unique"] == [["email"]] and shape["hr.no_pk"][2] == {}  # no key at all
    types = {c["column_name"]: (c["data_type"], c["is_nullable"]) for c in shape["sales.customer"][3]["columns"]}
    assert types["id"] == ("integer", False) and types["balance"] == ("numeric(12,2)", True)
    assert {i["index_name"] for i in shape["sales.order_line"][3]["indexes"]} == {
        "order_line_pkey",
        "ix_order_line_customer",
    }
    assert {s["schema_name"] for s in service.schemas(owner, made["id"])} == {"hr", "sales"}  # no pg_catalog, no public
    references = {(r["object_name"], r["ref_object"]) for r in service.relationships(owner, made["id"])}
    assert references == {("order_line", "customer"), ("order_note", "order_line")}


def test_postgres_the_driver_is_read_only_even_for_a_superuser(pg):
    target = Target("127.0.0.1", ("127.0.0.1",), pg.port, "ext", "postgres", "disable")
    import psycopg

    with postgres._connection(target, ext.ADMIN_PASSWORD, 5) as conn:
        for statement in (
            "CREATE TABLE sales.intruder (a int)",
            "INSERT INTO sales.customer VALUES (9, 'x', 'y', 1)",
            "UPDATE sales.customer SET name = 'x'",
            "DELETE FROM sales.customer",
            "DROP TABLE sales.customer",
            "TRUNCATE sales.customer",
            "CREATE ROLE intruder",
        ):
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                conn.execute(statement)
            conn.rollback()
    assert pg.scalar("SELECT count(*) FROM sales.customer") == "1"


def test_postgres_no_external_row_is_copied_and_nothing_external_changes(pg, service, owner):
    before = pg.checksum()
    # even a full account
    made = service.create(owner, pg_payload(pg, username="postgres", password=ext.ADMIN_PASSWORD))
    service.test(owner, made["id"])
    service.discover(owner, made["id"])
    service.discover(owner, made["id"])
    assert pg.checksum() == before
    with admin() as conn:
        everything = " ".join(
            r["t"]
            for table in (
                "connections",
                "connection_objects",
                "connection_columns",
                "connection_constraints",
                "connection_indexes",
                "connection_descriptions",
            )
            for r in conn.execute(f"SELECT row_to_json(x)::text AS t FROM {table} x")
        )
    assert ext.SECRET_ROW not in everything and "Hidden Customer Name" not in everything
    assert ext.ADMIN_PASSWORD not in everything


def test_postgres_a_discovery_that_is_too_slow_is_cut_off_and_keeps_the_previous_one(pg, service, owner, monkeypatch):
    made = service.create(owner, pg_payload(pg))
    service.discover(owner, made["id"])
    monkeypatch.setattr(config, "CONNECTOR_DISCOVERY_TIMEOUT_S", 0.000001)
    with pytest.raises(ConnectorError) as failed:
        service.discover(owner, made["id"])
    assert failed.value.code == "discovery_failed" and failed.value.reason == "timeout"
    assert service.objects(owner, made["id"])[1] == 8


def test_postgres_a_large_schema_is_discovered_in_batches(pg, service, owner):
    pg.sql(
        "CREATE SCHEMA bulk; DO $$ BEGIN FOR i IN 1..1500 LOOP "
        "EXECUTE format('CREATE TABLE bulk.t%s (id int, v text)', "
        "i); END LOOP; END $$;"
    )
    try:
        made = service.create(owner, pg_payload(pg))
        assert service.discover(owner, made["id"])["objects"] == 8 + 1500
        rows, total = service.objects(owner, made["id"], schema="bulk", limit=500, offset=1000)
        assert total == 1500 and len(rows) == 500 and rows[0]["columns"] == 2
    finally:
        pg.sql("DROP SCHEMA bulk CASCADE;")


def test_postgres_a_hostile_connection_name_or_database_is_only_data(pg, service, owner):
    hostile = pg_payload(pg, database_name="ext'; DROP TABLE sales.customer; --", name="a'b\"c")
    made, result = outcome(service, owner, hostile)
    assert result["error"] == "database_not_found" and made["name"] == "a'b\"c"
    assert pg.scalar("SELECT count(*) FROM sales.customer") == "1"


# ---------- PostgreSQL with TLS ----------


def test_postgres_tls_a_checked_certificate_is_accepted_and_a_wrong_one_is_not(pg_tls, certificates, service, owner):
    ca, _certificate, _key = certificates
    base = pg_payload(pg_tls, host="localhost", ssl_mode="verify-full", options={"ca_certificate_pem": ca})
    assert outcome(service, owner, base)[1]["status"] == "ok"
    assert outcome(service, owner, {**base, "name": "ca", "ssl_mode": "verify-ca"})[1]["status"] == "ok"
    other_ca, _c, _k = ext.make_certificates()
    wrong = outcome(service, owner, {**base, "name": "wrong", "options": {"ca_certificate_pem": other_ca}})[1]
    assert wrong["status"] == "failed" and wrong["error"] == "tls_failed"
    # the system roots do not know this CA
    no_ca = outcome(service, owner, {**base, "name": "system", "options": {}})[1]
    assert no_ca["error"] == "tls_failed"


def test_postgres_tls_the_name_in_the_certificate_is_checked_but_the_address_is_what_is_connected_to(
    docker_ready, service, owner
):
    ca, certificate, key = ext.make_certificates(names=("db.internal.test",), addresses=())
    server = ext.Postgres("postgres:17-alpine", tls=(certificate, key)).start()
    try:
        hosts = {"localhost": "tls_failed"}  # the certificate is for another name: verify-full refuses it
        for host, expected in hosts.items():
            result = outcome(
                service,
                owner,
                pg_payload(server, host=host, ssl_mode="verify-full", name=host, options={"ca_certificate_pem": ca}),
            )[1]
            assert result["error"] == expected
        assert (
            outcome(
                service,
                owner,
                pg_payload(
                    server, host="localhost", ssl_mode="verify-ca", name="ca", options={"ca_certificate_pem": ca}
                ),
            )[1]["status"]
            == "ok"
        )
    finally:
        server.stop()


def test_postgres_tls_without_checking_a_remote_host_is_refused_up_front(pg_tls, service, owner):
    refused = pg_payload(pg_tls, host="192.168.1.50", ssl_mode="require")
    with pytest.raises(ConnectorError) as error:
        service.create(owner, refused)
    assert error.value.code == "tls_required"


# ---------- the credential vault with a real database ----------


def test_the_password_is_encrypted_in_the_database_and_the_backup_of_it_holds_no_plaintext(pg, service, owner, keys):
    made = service.create(owner, pg_payload(pg))
    dump = " ".join(str(r) for r in repository.all_sealed(owner)) + str(repository.get_sealed(owner, made["id"]))
    assert ext.READER_PASSWORD not in dump
    assert keys.vault.open(repository.get_sealed(owner, made["id"]), made["id"]) == ext.READER_PASSWORD


# ---------- SQL Server: connection tests and discovery ----------


def test_sqlserver_a_good_connection_is_tested_and_a_writing_account_is_warned_about(mssql_server, service, owner):
    _, ok = outcome(service, owner, mssql_payload(mssql_server))
    assert ok["status"] == "ok" and ok["warning"] is None and ok["server_version"].startswith("16.")
    _, writer = outcome(
        service, owner, mssql_payload(mssql_server, username="writer", password=ext.WRITER_PASSWORD, name="w")
    )
    assert writer["warning"] == "account_can_write"


@pytest.mark.parametrize(
    "change,code",
    [
        ({"password": "not-the-password-0000"}, "auth_failed"),
        ({"username": "nobody"}, "auth_failed"),
        ({"database_name": "nodb"}, "database_not_found"),
    ],
)
def test_sqlserver_failures_are_fixed_codes(mssql_server, service, owner, caplog, change, code):
    caplog.set_level("DEBUG")
    made, result = outcome(service, owner, mssql_payload(mssql_server, **change))
    assert result["error"] == code
    shown = json.dumps([result, service.get(owner, made["id"])], default=str) + caplog.text
    assert "not-the-password-0000" not in shown and ext.READER_PASSWORD not in shown and "Login failed" not in shown


def test_sqlserver_a_closed_port_is_unreachable_and_a_silent_server_times_out(service, owner):
    free = stack_tools.free_port()
    assert (
        outcome(
            service,
            owner,
            {
                "name": "c",
                "provider": "mssql",
                "host": "127.0.0.1",
                "port": free,
                "database_name": "x",
                "username": "u",
                "password": "pw-123456",
                "ssl_mode": "disable",
            },
        )[1]["error"]
        == "unreachable"
    )
    quiet = socket.socket()
    quiet.bind(("127.0.0.1", 0))
    quiet.listen(5)
    try:
        result = outcome(
            service,
            owner,
            {
                "name": "q",
                "provider": "mssql",
                "host": "127.0.0.1",
                "port": quiet.getsockname()[1],
                "database_name": "x",
                "username": "u",
                "password": "pw-123456",
                "ssl_mode": "disable",
                "options": {"connect_timeout_s": 2},
            },
        )[1]
        assert result["error"] == "timeout"
    finally:
        quiet.close()


def test_sqlserver_discovery_finds_schemas_tables_views_columns_and_keys(mssql_server, service, owner):
    made = service.create(owner, mssql_payload(mssql_server))
    found = service.discover(owner, made["id"])
    assert found["objects"] == 6
    shape = tree(service, owner, made["id"])
    assert set(shape) == {
        "hr.employee",
        "hr.no_pk",
        "sales.big_orders",
        "sales.customer",
        "sales.order_line",
        "sales.order_note",
    }
    assert shape["sales.big_orders"][0] == "view"
    assert shape["sales.customer"][1] == ["id", "name", "secret_note", "balance"]
    types = {c["column_name"]: (c["data_type"], c["is_nullable"]) for c in shape["sales.customer"][3]["columns"]}
    assert types["name"] == ("nvarchar(100)", False) and types["secret_note"] == ("varchar(max)", True)
    assert types["balance"] == ("decimal(12,2)", True)
    assert shape["sales.order_line"][2]["primary_key"] == [["order_id", "line_no"]]
    assert shape["sales.order_note"][2]["foreign_key"] == [["order_id", "line_no"]]
    fk = shape["sales.order_note"][3]["constraints"][0]
    assert (fk["ref_schema"], fk["ref_object"], fk["ref_columns"]) == ("sales", "order_line", ["order_id", "line_no"])
    assert shape["hr.employee"][2]["unique"] == [["email"]] and shape["hr.no_pk"][2] == {}
    assert {s["schema_name"] for s in service.schemas(owner, made["id"])} == {"hr", "sales"}
    assert {(r["object_name"], r["ref_object"]) for r in service.relationships(owner, made["id"])} == {
        ("order_line", "customer"),
        ("order_note", "order_line"),
    }
    assert {i["index_name"] for i in shape["sales.order_line"][3]["indexes"]} >= {
        "pk_order_line",
        "ix_order_line_customer",
    }


def test_sqlserver_an_account_that_may_see_nothing_discovers_nothing_without_an_error(mssql_server, service, owner):
    made = service.create(owner, mssql_payload(mssql_server, username="blind", name="blind"))
    assert service.test(owner, made["id"])["status"] == "ok"
    assert service.discover(owner, made["id"])["objects"] == 0 and service.objects(owner, made["id"])[1] == 0


def test_sqlserver_no_external_row_is_copied_and_nothing_external_changes(mssql_server, service, owner):
    before = mssql_server.checksum()
    made = service.create(owner, mssql_payload(mssql_server, username="writer", password=ext.WRITER_PASSWORD))
    service.test(owner, made["id"])
    service.discover(owner, made["id"])
    assert mssql_server.checksum() == before
    with admin() as conn:
        everything = " ".join(
            r["t"]
            for table in (
                "connections",
                "connection_objects",
                "connection_columns",
                "connection_constraints",
                "connection_indexes",
                "connection_descriptions",
            )
            for r in conn.execute(f"SELECT row_to_json(x)::text AS t FROM {table} x")
        )
    assert ext.SECRET_ROW not in everything and ext.WRITER_PASSWORD not in everything


def test_sqlserver_a_hostile_database_name_is_only_data(mssql_server, service, owner):
    made, result = outcome(
        service, owner, mssql_payload(mssql_server, database_name="rag_ext]; DROP TABLE sales.customer; --")
    )
    assert result["error"] in ("database_not_found", "auth_failed")
    assert mssql_server.scalar("SELECT count(*) FROM sales.customer") == "1"


def test_sqlserver_two_operations_at_once_on_different_connections_both_complete(mssql_server, service, owner):
    first = service.create(owner, mssql_payload(mssql_server, name="one"))
    second = service.create(owner, mssql_payload(mssql_server, name="two"))
    results = {}
    threads = [
        threading.Thread(target=lambda n=c: results.update({n["name"]: service.discover(owner, n["id"])}))
        for c in (first, second)
    ]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert {k: v["objects"] for k, v in results.items()} == {"one": 6, "two": 6}


# ---------- SQL Server with TLS (restarts the container: keep these last) ----------


def test_sqlserver_tls_a_checked_certificate_is_accepted_and_a_wrong_name_or_ca_is_not(
    mssql_tls, certificates, service, owner
):
    ca, _certificate, _key = certificates
    mssql_server = mssql_tls
    base = mssql_payload(mssql_server, host="localhost", ssl_mode="verify-full", options={"ca_certificate_pem": ca})
    first = outcome(service, owner, base)[1]
    assert first["status"] == "ok", first
    assert outcome(service, owner, {**base, "name": "ca", "ssl_mode": "verify-ca"})[1]["status"] == "ok"
    other_ca, _c, _k = ext.make_certificates()
    assert (
        outcome(service, owner, {**base, "name": "wrong", "options": {"ca_certificate_pem": other_ca}})[1]["error"]
        == "tls_failed"
    )
    made = service.create(owner, {**base, "name": "disc"})
    assert service.discover(owner, made["id"])["objects"] == 6


def test_sqlserver_tls_a_server_that_requires_encryption_refuses_a_plain_client(mssql_tls, service, owner):
    result = outcome(service, owner, mssql_payload(mssql_tls, name="plain"))[1]
    assert result["status"] == "failed" and result["error"] in ("tls_failed", "unreachable", "failed")


def test_sqlserver_tls_needs_a_ca_certificate_and_has_no_encrypt_without_checking(service, owner):
    for mode, expected in (
        ("verify-full", "ca_required"),
        ("verify-ca", "ca_required"),
        ("require", "invalid_ssl_mode"),
    ):
        with pytest.raises(ConnectorError) as error:
            service.create(
                owner,
                {
                    "name": "x",
                    "provider": "mssql",
                    "host": "127.0.0.1",
                    "port": 1433,
                    "database_name": "d",
                    "username": "u",
                    "password": "pw-123456",
                    "ssl_mode": mode,
                },
            )
        assert error.value.code == expected


def test_driver_errors_are_driver_errors_and_nothing_else(mssql_server):
    target = Target("127.0.0.1", ("127.0.0.1",), mssql_server.port, "rag_ext", "reader", "disable")
    with pytest.raises(DriverError):
        mssql.test(target, "wrong-password-000", 5)
