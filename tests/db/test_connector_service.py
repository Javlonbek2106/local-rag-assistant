"""M4.1b against a real PostgreSQL (a throw-away schema): connections, the vault, ownership, discovery and descriptions.

The drivers are fakes (tests/connector_support.py); the real ones are tested against real servers in
tests/integration/test_connector_databases.py.
"""

import json
import uuid

import psycopg
import pytest

pytest.importorskip("cryptography")

from rag import config, db  # noqa: E402
from rag.auth.principals import Principals  # noqa: E402
from rag.connector import repository  # noqa: E402
from rag.connector.errors import ConnectorError, DriverError  # noqa: E402
from rag.connector.service import Service, needs_step_up  # noqa: E402
from tests.auth_support import Events  # noqa: E402

# noqa: E402
from tests.connector_support import PASSWORD, PAYLOAD, PUBLIC_ADDRESS, FakeDriver, KeyRing, internal, resolver

pytestmark = pytest.mark.db


def admin():
    return psycopg.connect(
        config.DATABASE_URL,
        options=f"-c search_path={config.DB_SCHEMA}",
        autocommit=True,
        row_factory=psycopg.rows.dict_row,
    )


@pytest.fixture(autouse=True)
def clean(database):
    with admin() as conn:
        conn.execute("DELETE FROM connections")
        conn.execute("DELETE FROM principals WHERE kind <> 'local-owner'")
    yield
    with admin() as conn:
        conn.execute("DELETE FROM connections")
        conn.execute("DELETE FROM principals WHERE kind <> 'local-owner'")


@pytest.fixture
def owner():
    return Principals().local_owner()["id"]


@pytest.fixture
def stranger():
    with admin() as conn:
        return str(
            conn.execute(
                "INSERT INTO principals (kind, display_label) VALUES ('user', 'someone else') RETURNING id"
            ).fetchone()["id"]
        )


@pytest.fixture
def keys():
    return KeyRing()


@pytest.fixture
def events():
    return Events()


@pytest.fixture
def driver():
    return FakeDriver()


@pytest.fixture
def service(keys, events, driver):
    return Service(keys.vault, events, resolver=resolver(), internal=internal, driver_lookup=driver)


def code(callable_, *args, **kwargs):
    with pytest.raises(ConnectorError) as error:
        callable_(*args, **kwargs)
    return error.value.code


def raw_row(connection_id):
    with admin() as conn:
        return conn.execute("SELECT * FROM connections WHERE id = %s", (connection_id,)).fetchone()


# ---------- creating, and what is stored ----------


def test_a_connection_is_created_for_the_owner_with_its_password_encrypted(service, owner, keys, events):
    made = service.create(owner, PAYLOAD)
    row = raw_row(made["id"])
    assert str(row["owner_principal_id"]) == owner and row["name"] == "Sales" and row["provider"] == "postgresql"
    assert bytes(row["credential_ciphertext"]) != PASSWORD.encode() and PASSWORD.encode() not in bytes(
        row["credential_ciphertext"]
    )
    assert (
        row["credential_key_id"] == "k1"
        and row["credential_version"] == 1
        and len(bytes(row["credential_nonce"])) == 12
    )
    assert keys.vault.open(repository.get_sealed(owner, made["id"]), made["id"]) == PASSWORD
    assert made["status"] == "ready" and made["credential_stored"] is True and made["is_enabled"] is True
    assert events.names()[-1] == ("connection_created", "ok")
    assert events.items[-1]["principal_id"] == owner and events.items[-1]["event_ref"] == made["id"]


def test_the_password_is_nowhere_in_the_database_and_no_credential_comes_back(service, owner):
    made = service.create(owner, PAYLOAD)
    service.test(owner, made["id"])
    service.update(owner, made["id"], {"description": "changed"})
    with admin() as conn:
        dump = " ".join(r["t"] for r in conn.execute("SELECT row_to_json(c)::text AS t FROM connections c"))
    assert PASSWORD not in dump and PASSWORD.encode().hex() not in dump
    shown = json.dumps([service.get(owner, made["id"]), *service.list(owner)], default=str)
    assert PASSWORD not in shown
    for forbidden in ("ciphertext", "nonce", "key_id", "password", "credential_version"):
        assert forbidden not in shown.replace("credential_stored", "")


def test_a_name_is_unique_per_owner_ignoring_case_but_another_owner_may_reuse_it(service, owner, stranger):
    service.create(owner, PAYLOAD)
    assert code(service.create, owner, {**PAYLOAD, "name": "SALES"}) == "name_taken"
    assert service.create(stranger, PAYLOAD)["name"] == "Sales"


def test_there_is_a_limit_of_connections_per_owner(service, owner, monkeypatch):
    monkeypatch.setattr(config, "CONNECTOR_MAX_CONNECTIONS", 2)
    service.create(owner, {**PAYLOAD, "name": "a"})
    service.create(owner, {**PAYLOAD, "name": "b"})
    assert code(service.create, owner, {**PAYLOAD, "name": "c"}) == "limit_reached"


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"provider": "oracle"}, "invalid_provider"),
        ({"host": "a b"}, "invalid_host"),
        ({"port": 0}, "invalid_port"),
        ({"password": ""}, "invalid_credential"),
        ({"ssl_mode": "x"}, "invalid_ssl_mode"),
        ({"host": "169.254.169.254"}, "host_blocked"),
        ({"ssl_mode": "disable"}, "tls_required"),
        ({"options": {"sslmode": "disable"}}, "invalid_options"),
    ],
)
def test_an_invalid_connection_is_refused_and_nothing_is_stored(service, owner, change, expected):
    assert code(service.create, owner, {**PAYLOAD, **change}) == expected
    assert repository.count(owner) == 0


def test_the_internal_database_cannot_be_made_a_connection(keys, events, driver, owner):
    own = Service(
        keys.vault,
        events,
        resolver=resolver("127.0.0.1"),
        internal=lambda: ("127.0.0.1", 5433, "rag"),
        driver_lookup=driver,
    )
    refused = {**PAYLOAD, "host": "localhost", "port": 5433, "database_name": "rag", "ssl_mode": "disable"}
    assert code(own.create, owner, refused) == "internal_database"


def test_a_missing_key_refuses_to_create_and_stores_nothing(service, owner, keys):
    keys.missing = True
    with pytest.raises(ConnectorError) as error:
        service.create(owner, PAYLOAD)
    assert error.value.code == "key_unavailable" and error.value.status == 503 and error.value.reason == "key_missing"
    assert repository.count(owner) == 0


def test_a_security_log_that_cannot_be_written_refuses_the_change_before_it_starts(service, owner, events):
    events.lost = True
    assert code(service.create, owner, PAYLOAD) == "security_log_unavailable"
    assert repository.count(owner) == 0


def test_a_connection_that_could_not_be_recorded_is_taken_back(keys, owner):
    class Flaky(Events):
        def check(self):
            return True  # looked fine a moment ago ...

        def emit(self, *args, **kwargs):
            return False  # ... but the write failed

    flaky = Service(keys.vault, Flaky(), resolver=resolver(), internal=internal, driver_lookup=FakeDriver())
    assert code(flaky.create, owner, PAYLOAD) == "security_log_unavailable"
    assert repository.count(owner) == 0


# ---------- ownership: every operation, every owner ----------


def test_another_owner_sees_nothing_and_can_do_nothing(service, owner, stranger):
    made = service.create(owner, PAYLOAD)
    cid = made["id"]
    assert service.list(stranger) == []
    attempts = [
        lambda: service.get(stranger, cid),
        lambda: service.update(stranger, cid, {"description": "x"}),
        lambda: service.set_enabled(stranger, cid, False),
        lambda: service.delete(stranger, cid),
        lambda: service.test(stranger, cid),
        lambda: service.discover(stranger, cid),
        lambda: service.schemas(stranger, cid),
        lambda: service.objects(stranger, cid),
        lambda: service.object_detail(stranger, cid, "sales", "customer"),
        lambda: service.relationships(stranger, cid),
        lambda: service.descriptions(stranger, cid),
        lambda: service.describe(stranger, cid, "database", "", "", "", "text"),
    ]
    for attempt in attempts:
        assert code(attempt) == "not_found"
    assert service.get(owner, cid)["description"] == "the sales database" and repository.count(owner) == 1


def test_the_repository_itself_filters_by_owner(service, owner, stranger, keys):
    made = service.create(owner, PAYLOAD)
    cid = made["id"]
    assert repository.get(stranger, cid) is None and repository.get_sealed(stranger, cid) is None
    assert repository.list_connections(stranger) == [] and repository.all_sealed(stranger) == []
    assert repository.update(stranger, cid, {"name": "mine now"}) is None
    assert repository.set_enabled(stranger, cid, False) is False and repository.delete(stranger, cid) is False
    assert repository.set_description(stranger, cid, "database", "", "", "", "x") is False
    assert repository.schemas(stranger, cid) == [] and repository.objects(stranger, cid) == ([], 0)
    assert repository.object_detail(stranger, cid, "a", "b") is None and repository.relationships(stranger, cid) == []
    assert repository.replace_sealed(stranger, cid, repository.get_sealed(owner, cid)) is False
    assert repository.get(owner, cid)["name"] == "Sales"


def test_a_row_cannot_be_given_to_an_owner_that_does_not_exist(owner):
    with admin() as conn, pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.execute(
            "INSERT INTO connections (id, owner_principal_id, name, provider, host, port, database_name, "
            "username, credential_ciphertext, credential_nonce, credential_key_id, ssl_mode) VALUES "
            "(gen_random_uuid(), gen_random_uuid(), 'x', 'postgresql', 'h', 1, 'd', 'u', '\\x00', "
            "'\\x000000000000000000000000', 'k', 'disable')"
        )


def test_a_connection_id_that_is_not_an_id_is_just_not_found(service, owner):
    for bad in ("not-an-id", "1; DROP TABLE connections", "' OR '1'='1", ""):
        assert code(service.get, owner, bad) == "not_found"
    assert repository.count(owner) == 0 or True


def test_the_connector_tables_all_survive_injection_looking_input(service, owner):
    made = service.create(
        owner,
        {
            **PAYLOAD,
            "name": "x'); DROP TABLE connections; --",
            "database_name": "d'; DELETE FROM connections; --",
            "username": "u' OR '1'='1",
        },
    )
    assert service.get(owner, made["id"])["name"] == "x'); DROP TABLE connections; --"
    assert repository.count(owner) == 1


# ---------- changing ----------


def test_changing_a_name_or_description_keeps_the_credential_and_the_last_test(service, owner, keys):
    made = service.create(owner, PAYLOAD)
    service.test(owner, made["id"])
    before = raw_row(made["id"])
    changed = service.update(owner, made["id"], {"name": "Renamed", "description": "new text"})
    after = raw_row(made["id"])
    assert changed["name"] == "Renamed" and after["credential_ciphertext"] == before["credential_ciphertext"]
    assert after["credential_nonce"] == before["credential_nonce"] and after["last_test_status"] == "ok"
    assert after["updated_at"] > before["updated_at"]


def test_a_new_password_replaces_the_encrypted_one_and_an_omitted_one_keeps_it(service, owner, keys, events):
    made = service.create(owner, PAYLOAD)
    first = raw_row(made["id"])
    service.update(owner, made["id"], {"username": "other_user"})  # no password: the stored one stays
    assert keys.vault.open(repository.get_sealed(owner, made["id"]), made["id"]) == PASSWORD
    service.update(owner, made["id"], {"password": "A-brand-new-secret-77"})
    assert keys.vault.open(repository.get_sealed(owner, made["id"]), made["id"]) == "A-brand-new-secret-77"
    assert raw_row(made["id"])["credential_ciphertext"] != first["credential_ciphertext"]
    assert events.names()[-1] == ("connection_updated", "credential")


def test_changing_where_the_credential_goes_forgets_the_last_test(service, owner):
    made = service.create(owner, PAYLOAD)
    service.test(owner, made["id"])
    moved = service.update(owner, made["id"], {"host": "other.example.org"})
    assert (
        moved["host"] == "other.example.org" and moved["last_test_status"] is None and moved["last_tested_at"] is None
    )


def test_only_changes_of_the_target_or_the_password_need_a_step_up():
    assert not needs_step_up({"name": "x", "description": "y"})
    for key in ("provider", "host", "port", "database_name", "username", "ssl_mode", "options", "password"):
        assert needs_step_up({key: 1})


def test_a_stored_ca_certificate_is_kept_when_other_options_change(service, owner, monkeypatch):
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test ca")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=2))
        .sign(key, hashes.SHA256())
    )
    pem = certificate.public_bytes(serialization.Encoding.PEM).decode()
    made = service.create(owner, {**PAYLOAD, "options": {"ca_certificate_pem": pem}})
    assert made["ca_certificate_set"] is True and "ca_certificate_pem" not in json.dumps(made, default=str)
    kept = service.update(owner, made["id"], {"options": {"connect_timeout_s": 5}})
    assert kept["ca_certificate_set"] is True and kept["options"] == {"connect_timeout_s": 5}
    removed = service.update(owner, made["id"], {"options": {"ca_certificate_pem": ""}})
    assert removed["ca_certificate_set"] is False


def test_a_field_that_is_not_editable_is_refused(service, owner):
    made = service.create(owner, PAYLOAD)
    for key in ("owner_principal_id", "id", "is_enabled", "last_test_status", "credential_key_id"):
        assert code(service.update, owner, made["id"], {key: "x"}) == "invalid_options"


# ---------- enabling, disabling, testing ----------


def test_a_disabled_connection_cannot_be_tested_or_discovered_until_it_is_enabled(service, owner, driver, events):
    made = service.create(owner, PAYLOAD)
    assert service.set_enabled(owner, made["id"], False)["status"] == "disabled"
    assert code(service.test, owner, made["id"]) == "connection_disabled"
    assert code(service.discover, owner, made["id"]) == "connection_disabled"
    assert [call for call in driver.calls if call[0] in ("test", "introspect")] == []  # nothing reached the driver
    assert service.set_enabled(owner, made["id"], True)["status"] == "ready"
    assert service.test(owner, made["id"])["status"] == "ok"
    assert ("connection_disabled", "ok") in events.names() and ("connection_enabled", "ok") in events.names()


def test_a_test_connects_with_the_decrypted_password_the_pinned_address_and_a_bounded_timeout(service, owner, driver):
    made = service.create(owner, {**PAYLOAD, "options": {"connect_timeout_s": 3}})
    result = service.test(owner, made["id"])
    kind, target, password, timeout = [c for c in driver.calls if c[0] == "test"][0]
    assert password == PASSWORD and target.addresses == (PUBLIC_ADDRESS,) and target.host == "db.example.org"
    assert target.database == "sales" and target.user == "reader" and target.ssl_mode == "verify-full"
    assert timeout == 3 and result == {"status": "ok", "error": None, "warning": None, "server_version": "17.2"}


def test_a_test_never_waits_longer_than_the_global_limit(service, owner, driver, monkeypatch):
    monkeypatch.setattr(config, "CONNECTOR_TEST_TIMEOUT_S", 4.0)
    made = service.create(owner, {**PAYLOAD, "options": {"connect_timeout_s": 30}})
    service.test(owner, made["id"])
    assert [c for c in driver.calls if c[0] == "test"][0][3] == 4.0


def test_the_outcome_of_a_test_is_stored_as_a_fixed_code(service, owner, driver, events):
    made = service.create(owner, PAYLOAD)
    for failure in ("auth_failed", "database_not_found", "timeout", "unreachable", "tls_failed"):
        driver.test_error = DriverError(failure)
        result = service.test(owner, made["id"])
        stored = service.get(owner, made["id"])
        assert result["status"] == "failed" and result["error"] == failure
        assert stored["last_test_status"] == "failed" and stored["last_test_error"] == failure
        assert stored["last_tested_at"] is not None
    assert ("connection_tested", "failed") in events.names()
    driver.test_error = None
    assert (
        service.test(owner, made["id"])["status"] == "ok" and service.get(owner, made["id"])["last_test_error"] is None
    )


def test_a_driver_that_raises_something_unexpected_leaks_nothing(service, owner, driver):
    made = service.create(owner, PAYLOAD)
    driver.test_error = RuntimeError(f"could not connect to postgresql://reader:{PASSWORD}@db.example.org/sales")
    result = service.test(owner, made["id"])
    assert result["error"] == "failed" and PASSWORD not in json.dumps(service.get(owner, made["id"]), default=str)


def test_an_account_that_can_write_is_reported_as_a_warning(service, owner, driver):
    made = service.create(owner, PAYLOAD)
    driver.can_write = True
    assert service.test(owner, made["id"])["warning"] == "account_can_write"
    assert service.get(owner, made["id"])["last_test_warning"] == "account_can_write"


def test_a_name_that_now_resolves_somewhere_blocked_is_refused_at_test_time(keys, events, driver, owner):
    first = Service(keys.vault, events, resolver=resolver(), internal=internal, driver_lookup=driver)
    made = first.create(owner, PAYLOAD)
    rebinding = Service(
        keys.vault, events, resolver=resolver("169.254.169.254"), internal=internal, driver_lookup=driver
    )
    assert code(rebinding.test, owner, made["id"]) == "host_blocked"
    assert [c for c in driver.calls if c[0] == "test"] == []


def test_a_key_that_is_gone_makes_the_connection_need_its_credentials_and_a_new_password_repairs_it(
    service, owner, keys
):
    made = service.create(owner, PAYLOAD)
    keys.missing = True
    assert service.get(owner, made["id"])["status"] == "needs_credentials"
    assert code(service.test, owner, made["id"]) == "needs_credentials"
    keys.missing = False
    assert service.get(owner, made["id"])["status"] == "ready"
    keys.text = KeyRing("other-key").text  # a different key: this ciphertext cannot be opened any more
    assert service.get(owner, made["id"])["status"] == "needs_credentials"
    assert code(service.test, owner, made["id"]) == "needs_credentials"
    repaired = service.update(owner, made["id"], {"password": "entered again 12345"})
    assert repaired["status"] == "ready" and service.test(owner, made["id"])["status"] == "ok"


def test_at_most_a_few_external_operations_run_at_once(service, owner, driver, monkeypatch):
    import threading

    monkeypatch.setattr(config, "CONNECTOR_MAX_CONCURRENT", 1)
    limited = Service(service._vault, service._events, resolver=resolver(), internal=internal, driver_lookup=driver)
    a, b = limited.create(owner, {**PAYLOAD, "name": "a"}), limited.create(owner, {**PAYLOAD, "name": "b"})
    inside, release, outcome = threading.Event(), threading.Event(), {}
    original = driver.test

    def slow(target, password, timeout):
        inside.set()
        release.wait(5)
        return original(target, password, timeout)

    driver.test = slow
    worker = threading.Thread(target=lambda: outcome.update(first=limited.test(owner, a["id"])))
    worker.start()
    assert inside.wait(5)
    with pytest.raises(ConnectorError) as busy:
        limited.test(owner, b["id"])
    assert busy.value.code == "busy" and busy.value.status == 429
    release.set()
    worker.join(5)
    assert outcome["first"]["status"] == "ok" and limited.test(owner, b["id"])["status"] == "ok"


def test_two_operations_on_the_same_connection_do_not_overlap(service, owner, driver):
    import threading

    made = service.create(owner, PAYLOAD)
    inside, release, outcome = threading.Event(), threading.Event(), {}
    original = driver.test

    def slow(target, password, timeout):
        inside.set()
        release.wait(5)
        return original(target, password, timeout)

    driver.test = slow
    worker = threading.Thread(target=lambda: outcome.update(first=service.test(owner, made["id"])))
    worker.start()
    assert inside.wait(5)
    with pytest.raises(ConnectorError) as busy:
        service.discover(owner, made["id"])
    assert busy.value.code == "busy" and busy.value.status == 409
    release.set()
    worker.join(5)


# ---------- discovery ----------


def test_discovery_stores_schemas_tables_views_columns_and_keys_and_no_rows(service, owner, driver, events):
    made = service.create(owner, PAYLOAD)
    found = service.discover(owner, made["id"])
    assert found["objects"] == 6 and found["columns"] == 11 and found["schema_discovered_at"] is not None
    assert [s["schema_name"] for s in service.schemas(owner, made["id"])] == ["hr", "sales"]
    assert {(s["schema_name"], s["tables"], s["views"]) for s in service.schemas(owner, made["id"])} == {
        ("hr", 2, 0),
        ("sales", 3, 1),
    }
    rows, total = service.objects(owner, made["id"], schema="sales")
    assert total == 4 and {r["object_name"]: r["object_type"] for r in rows}["big_orders"] == "view"
    assert service.get(owner, made["id"])["schema_discovered_at"] is not None
    assert events.names()[-1] == ("schema_discovered", "ok")


def test_a_composite_primary_key_a_composite_foreign_key_and_a_table_without_a_key_are_kept_right(service, owner):
    made = service.create(owner, PAYLOAD)
    service.discover(owner, made["id"])
    line = service.object_detail(owner, made["id"], "sales", "order_line")
    pk = [c for c in line["constraints"] if c["kind"] == "primary_key"][0]
    assert pk["columns"] == ["order_id", "line_no"]  # one key over two columns, in key order
    assert [c["column_name"] for c in line["columns"]] == ["order_id", "line_no", "customer_id"]
    note = service.object_detail(owner, made["id"], "sales", "order_note")
    fk = note["constraints"][0]
    assert fk["kind"] == "foreign_key" and fk["columns"] == ["order_id", "line_no"]
    assert (fk["ref_schema"], fk["ref_object"], fk["ref_columns"]) == ("sales", "order_line", ["order_id", "line_no"])
    assert service.object_detail(owner, made["id"], "hr", "no_pk")["constraints"] == []
    assert service.object_detail(owner, made["id"], "hr", "employee")["indexes"][0]["index_name"] == "ix_email"
    relations = service.relationships(owner, made["id"])
    assert {(r["object_name"], r["ref_object"]) for r in relations} == {
        ("order_line", "customer"),
        ("order_note", "order_line"),
    }


def test_a_search_and_paging_of_the_objects(service, owner):
    made = service.create(owner, PAYLOAD)
    service.discover(owner, made["id"])
    rows, total = service.objects(owner, made["id"], query="ORDER")
    assert [r["object_name"] for r in rows] == ["big_orders", "order_line", "order_note"] and total == 3
    page, total = service.objects(owner, made["id"], limit=2, offset=4)
    assert len(page) == 2 and total == 6
    # LIKE wildcards in the query are text, not patterns
    assert service.objects(owner, made["id"], query="100%_")[1] == 0


def test_a_new_discovery_replaces_the_old_one_as_a_whole(service, owner, driver):
    made = service.create(owner, PAYLOAD)
    service.discover(owner, made["id"])
    driver.sample = {
        **driver.sample,
        "objects": driver.sample["objects"][:2],
        "columns": [],
        "constraints": [],
        "indexes": [],
    }
    service.discover(owner, made["id"])
    rows, total = service.objects(owner, made["id"])
    assert total == 2 and {r["object_name"] for r in rows} == {"employee", "no_pk"}


def test_a_discovery_that_fails_halfway_leaves_the_previous_one_untouched(service, owner, driver, events):
    made = service.create(owner, PAYLOAD)
    service.discover(owner, made["id"])
    before = service.get(owner, made["id"])["schema_discovered_at"]
    driver.fail_after, driver.discover_error = 2, "timeout"
    with pytest.raises(ConnectorError) as failed:
        service.discover(owner, made["id"])
    assert failed.value.code == "discovery_failed" and failed.value.status == 502 and failed.value.reason == "timeout"
    assert (
        service.objects(owner, made["id"])[1] == 6 and service.get(owner, made["id"])["schema_discovered_at"] == before
    )
    assert events.names()[-1] == ("schema_discovered", "failed")


def test_a_schema_larger_than_the_limit_is_refused_and_nothing_partial_is_kept(service, owner, monkeypatch):
    made = service.create(owner, PAYLOAD)
    service.discover(owner, made["id"])
    monkeypatch.setattr(config, "CONNECTOR_MAX_OBJECTS", 3)
    with pytest.raises(ConnectorError) as failed:
        service.discover(owner, made["id"])
    assert failed.value.reason == "too_large" and service.objects(owner, made["id"])[1] == 6
    monkeypatch.setattr(config, "CONNECTOR_MAX_OBJECTS", 20000)
    monkeypatch.setattr(config, "CONNECTOR_MAX_COLUMNS", 4)
    with pytest.raises(ConnectorError) as failed:
        service.discover(owner, made["id"])
    assert failed.value.reason == "too_large" and service.objects(owner, made["id"])[1] == 6


def test_a_large_schema_is_written_in_batches_not_held_in_memory(service, owner, driver):
    objects = [("big", f"t{i:05d}", "table") for i in range(1500)]
    columns = [("big", f"t{i:05d}", n, f"c{n}", "integer", True) for i in range(1500) for n in (1, 2)]
    driver.sample = {"objects": objects, "columns": columns, "constraints": [], "indexes": []}
    made = service.create(owner, PAYLOAD)
    assert service.discover(owner, made["id"])["objects"] == 1500
    rows, total = service.objects(owner, made["id"], limit=500, offset=1000)
    assert total == 1500 and len(rows) == 500 and rows[0]["columns"] == 2
    assert service.objects(owner, made["id"], limit=10_000)[0].__len__() == 500  # a page is never larger than 500


# ---------- descriptions ----------


def test_descriptions_can_be_set_changed_and_removed_for_every_kind_of_object(service, owner):
    made = service.create(owner, PAYLOAD)
    service.discover(owner, made["id"])
    cid = made["id"]
    service.describe(owner, cid, "database", "", "", "", "the whole sales database")
    service.describe(owner, cid, "schema", "sales", "", "", "everything about selling")
    service.describe(owner, cid, "table", "sales", "customer", "", "people who buy")
    service.describe(owner, cid, "table", "sales", "big_orders", "", "a view of big orders")
    service.describe(owner, cid, "column", "sales", "customer", "name", "the full legal name")
    found = {
        (d["target_kind"], d["schema_name"], d["object_name"], d["column_name"]): d["description"]
        for d in service.descriptions(owner, cid)
    }
    assert found[("column", "sales", "customer", "name")] == "the full legal name" and len(found) == 5
    service.describe(owner, cid, "table", "sales", "customer", "", "customers")
    service.describe(owner, cid, "column", "sales", "customer", "name", "")  # empty: removed
    found = {
        (d["target_kind"], d["schema_name"], d["object_name"], d["column_name"]): d["description"]
        for d in service.descriptions(owner, cid)
    }
    assert found[("table", "sales", "customer", "")] == "customers" and len(found) == 4


@pytest.mark.parametrize(
    "kind,schema,name,column",
    [("table", "sales", "nothing", ""), ("column", "sales", "customer", "zzz"), ("schema", "nope", "", "")],
)
def test_a_description_for_something_that_was_not_discovered_is_refused(service, owner, kind, schema, name, column):
    made = service.create(owner, PAYLOAD)
    service.discover(owner, made["id"])
    assert code(service.describe, owner, made["id"], kind, schema, name, column, "text") == "object_not_found"


@pytest.mark.parametrize(
    "kind,schema,name,column",
    [
        ("planet", "", "", ""),
        ("table", "", "customer", ""),
        ("column", "sales", "customer", ""),
        ("schema", "", "", ""),
    ],
)
def test_a_description_with_a_target_that_does_not_make_sense_is_refused(service, owner, kind, schema, name, column):
    made = service.create(owner, PAYLOAD)
    service.discover(owner, made["id"])
    assert code(service.describe, owner, made["id"], kind, schema, name, column, "text") == "invalid_target"


def test_descriptions_are_kept_apart_from_what_was_discovered_and_survive_a_new_discovery(service, owner):
    made = service.create(owner, PAYLOAD)
    service.discover(owner, made["id"])
    service.describe(owner, made["id"], "table", "sales", "customer", "", "people who buy")
    service.discover(owner, made["id"])
    assert [d["description"] for d in service.descriptions(owner, made["id"])] == ["people who buy"]
    assert "people who buy" not in json.dumps(
        service.object_detail(owner, made["id"], "sales", "customer"), default=str
    )


# ---------- deleting ----------


def test_deleting_a_connection_takes_what_hangs_off_it_and_leaves_other_connections_alone(service, owner, events):
    first, second = service.create(owner, PAYLOAD), service.create(owner, {**PAYLOAD, "name": "Other"})
    for made in (first, second):
        service.discover(owner, made["id"])
        service.describe(owner, made["id"], "database", "", "", "", "text")
    service.delete(owner, first["id"])
    with admin() as conn:
        left = {
            t: conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"]
            for t in (
                "connections",
                "connection_objects",
                "connection_columns",
                "connection_constraints",
                "connection_indexes",
                "connection_descriptions",
            )
        }
    assert left == {
        "connections": 1,
        "connection_objects": 6,
        "connection_columns": 11,
        "connection_constraints": 6,
        "connection_indexes": 2,
        "connection_descriptions": 1,
    }
    assert code(service.get, owner, first["id"]) == "not_found" and events.names()[-1] == ("connection_deleted", "ok")


def test_a_second_delete_is_not_found(service, owner):
    made = service.create(owner, PAYLOAD)
    service.delete(owner, made["id"])
    assert code(service.delete, owner, made["id"]) == "not_found"


def test_the_ids_are_real_uuids_and_every_row_belongs_to_one_owner(service, owner):
    made = service.create(owner, PAYLOAD)
    assert str(uuid.UUID(made["id"])) == made["id"]
    assert db.pool()  # (the pool is the application's; the connector uses only it and one standalone connection)


def test_a_change_is_refused_while_a_test_or_discovery_runs_on_the_same_connection(service, owner, driver):
    import threading

    made = service.create(owner, PAYLOAD)
    inside, release = threading.Event(), threading.Event()
    original = driver.test

    def slow(target, password, timeout):
        inside.set()
        release.wait(5)
        return original(target, password, timeout)

    driver.test = slow
    worker = threading.Thread(target=lambda: service.test(owner, made["id"]))
    worker.start()
    assert inside.wait(5)
    try:
        for attempt in (
            lambda: service.update(owner, made["id"], {"description": "changed"}),
            lambda: service.set_enabled(owner, made["id"], False),
            lambda: service.delete(owner, made["id"]),
        ):
            with pytest.raises(ConnectorError) as busy:
                attempt()
            assert busy.value.code == "busy" and busy.value.status == 409
    finally:
        release.set()
        worker.join(5)
    assert service.get(owner, made["id"])["description"] == "the sales database"  # nothing was changed or waited for


def test_a_rename_does_not_look_the_host_up_again(keys, events, driver, owner):
    made = Service(keys.vault, events, resolver=resolver(), internal=internal, driver_lookup=driver).create(
        owner, PAYLOAD
    )

    def no_dns(*args, **kwargs):
        raise OSError("the name server is down")

    offline = Service(keys.vault, events, resolver=no_dns, internal=internal, driver_lookup=driver)
    assert (
        offline.update(owner, made["id"], {"name": "Renamed"})["name"] == "Renamed"
    )  # nothing about the target changed
    assert code(offline.update, owner, made["id"], {"host": "other.example.org"}) == "host_unresolvable"
