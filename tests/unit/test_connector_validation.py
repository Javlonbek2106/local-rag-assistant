"""M4.1b: what a connection may look like, and where it may point (SSRF, connection-string injection)."""

import socket
import time

import pytest

from rag import config
from rag.connector import network, validation
from rag.connector.errors import ConnectorError

GOOD = {
    "name": "Sales",
    "description": "the sales db",
    "provider": "postgresql",
    "host": "db.example.org",
    "port": 5432,
    "database_name": "sales",
    "username": "reader",
    "ssl_mode": "verify-full",
    "options": {},
}
INTERNAL = ("127.0.0.1", 5433, "rag")


def code(callable_, *args, **kwargs):
    with pytest.raises(ConnectorError) as error:
        callable_(*args, **kwargs)
    return error.value.code


def resolver(*addresses):
    def resolve(host, port, family=0, kind=0):
        return [
            (socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in addresses
        ]

    return resolve


def judge(host, port=5432, database="sales", user="reader", addresses=("93.184.216.34",), internal=INTERNAL):
    return network.check_target(host, port, database, user, resolver=resolver(*addresses), internal=lambda: internal)


# ---------- fields ----------


def test_a_good_connection_is_normalised():
    fields = validation.connection_fields({**GOOD, "host": " DB.Example.org. ", "name": "  Sales  "})
    assert fields["host"] == "db.example.org" and fields["name"] == "Sales" and fields["ssl_mode"] == "verify-full"


@pytest.mark.parametrize(
    "key,value,expected",
    [
        ("provider", "mysql", "invalid_provider"),
        ("provider", None, "invalid_provider"),
        ("name", "", "invalid_name"),
        ("name", "x" * 101, "invalid_name"),
        ("name", "a\nb", "invalid_name"),
        ("name", 5, "invalid_name"),
        ("port", 0, "invalid_port"),
        ("port", 70000, "invalid_port"),
        ("port", "5432", "invalid_port"),
        ("port", True, "invalid_port"),
        ("database_name", "", "invalid_database"),
        ("database_name", "x" * 129, "invalid_database"),
        ("database_name", "a\x00b", "invalid_database"),
        ("username", "", "invalid_username"),
        ("username", "u\rv", "invalid_username"),
        ("ssl_mode", "allow", "invalid_ssl_mode"),
        ("ssl_mode", "VERIFY-FULL", "invalid_ssl_mode"),
        ("description", "x" * 2001, "invalid_description"),
        ("description", "bad\x07", "invalid_description"),
        ("options", "sslmode=disable", "invalid_options"),
        ("options", {"sslmode": "disable"}, "invalid_options"),
        ("options", {"connect_timeout_s": 0}, "invalid_options"),
        ("options", {"connect_timeout_s": 31}, "invalid_options"),
        ("options", {"connect_timeout_s": True}, "invalid_options"),
        ("options", {"accept_insecure_tls": "yes"}, "invalid_options"),
    ],
)
def test_a_field_out_of_shape_is_refused_with_its_own_code(key, value, expected):
    assert code(validation.connection_fields, {**GOOD, key: value}) == expected


@pytest.mark.parametrize(
    "host",
    [
        "",
        " ",
        "a b",
        "host;sslmode=disable",
        "host,other",
        "host@other",
        "user@host",
        "host/path",
        "host?x=1",
        "host#frag",
        "ho'st",
        'ho"st',
        "ho\\st",
        "-bad.example",
        "bad-.example",
        "[::1]",
        "fe80::1%eth0",
        "host\nx",
        "ho\x00st",
        "é.example",
        "x" * 300,
        "host:5432",
        "::g",
        "1:2:3:4:5:6:7:8:9",
    ],
)
def test_a_host_that_could_be_read_as_something_else_is_refused(host):
    assert code(network.normalise_host, host) == "invalid_host"


@pytest.mark.parametrize(
    "host", ["localhost", "db-1.internal.example.org", "10.0.0.5", "192.168.1.20", "::1", "2001:db8::5", "a"]
)
def test_ordinary_hosts_and_address_literals_are_accepted(host):
    assert network.normalise_host(host) == host


def test_a_value_that_looks_like_connection_settings_is_just_a_strange_database_name():
    fields = validation.connection_fields({**GOOD, "database_name": "x sslmode=disable host=evil"})
    assert fields["database_name"] == "x sslmode=disable host=evil"  # it is never part of a connection string (drivers)


def test_the_password_keeps_its_spaces_and_is_bounded():
    assert validation.password("  two words  ") == "  two words  "
    for bad in ("", "a\x00b", "x" * 1025, None, 5):
        assert code(validation.password, bad) == "invalid_credential"


def test_unknown_options_are_refused_not_ignored():
    assert code(validation.options, {"connect_timeout_s": 5, "extra": 1}) == "invalid_options"
    assert validation.options(None) == {} and validation.options({"connect_timeout_s": 7}) == {"connect_timeout_s": 7}


def test_a_default_port_follows_the_provider():
    raw = {key: value for key, value in GOOD.items() if key != "port"}
    assert validation.connection_fields(raw)["port"] == 5432
    assert validation.connection_fields({**raw, "provider": "mssql"})["port"] == 1433


# ---------- encryption policy (SR-10) ----------


@pytest.mark.parametrize("mode", ["disable", "require"])
def test_a_weak_tls_mode_is_refused_for_a_host_elsewhere_unless_the_owner_says_so(mode):
    fields = validation.connection_fields({**GOOD, "ssl_mode": mode})
    assert code(validation.tls_policy, fields, False) == "tls_required"
    validation.tls_policy(fields, True)  # this computer: fine
    accepted = validation.connection_fields({**GOOD, "ssl_mode": mode, "options": {"accept_insecure_tls": True}})
    validation.tls_policy(accepted, False)  # said so, in the request itself


def test_the_default_is_the_strict_mode():
    raw = {key: value for key, value in GOOD.items() if key != "ssl_mode"}
    assert validation.connection_fields(raw)["ssl_mode"] == "verify-full"


def test_sql_server_has_no_encrypt_without_checking_and_needs_a_ca_for_tls():
    mssql = {**GOOD, "provider": "mssql", "port": 1433}
    assert (
        code(validation.tls_policy, validation.connection_fields({**mssql, "ssl_mode": "require"}), True)
        == "invalid_ssl_mode"
    )
    assert (
        code(validation.tls_policy, validation.connection_fields({**mssql, "ssl_mode": "verify-full"}), False)
        == "ca_required"
    )
    validation.tls_policy(validation.connection_fields({**mssql, "ssl_mode": "disable"}), True)


# ---------- where a connection may point ----------


def test_a_public_address_is_accepted_and_pinned():
    target = judge("db.example.org")
    assert target.addresses == ("93.184.216.34",) and not target.loopback and target.port == 5432


@pytest.mark.parametrize(
    "address",
    [
        "169.254.169.254",
        "169.254.0.1",
        "0.0.0.0",
        "::",
        "224.0.0.1",
        "255.255.255.255",
        "fe80::1",
        "ff02::1",
        "::ffff:169.254.169.254",
        "240.0.0.1",
    ],
)
def test_link_local_metadata_unspecified_multicast_and_reserved_addresses_are_blocked(address):
    assert (
        code(judge, address.replace("::ffff:", "") if address.startswith("::ffff") else address, addresses=(address,))
        == "host_blocked"
    )


def test_a_name_that_resolves_to_any_blocked_address_is_blocked():
    assert code(judge, "tricky.example.org", addresses=("93.184.216.34", "169.254.169.254")) == "host_blocked"


def test_the_cloud_metadata_name_resolves_into_the_block():
    assert code(judge, "metadata.google.internal", addresses=("169.254.169.254",)) == "host_blocked"


@pytest.mark.parametrize("address", ["127.0.0.1", "::1", "10.1.2.3", "172.16.5.5", "192.168.0.9", "fd00::1"])
def test_loopback_and_private_networks_are_allowed_on_purpose(address):
    target = judge("local-db", addresses=(address,))
    assert target.addresses == (address,) and target.loopback == (address in ("127.0.0.1", "::1"))


def test_a_name_that_does_not_resolve_is_refused_with_one_word():
    def nothing(*args):
        raise socket.gaierror("Name or service not known: db.example.org")

    assert (
        code(network.check_target, "db.example.org", 5432, "d", "u", resolver=nothing, internal=lambda: INTERNAL)
        == "host_unresolvable"
    )


def test_a_resolver_that_hangs_does_not_hang_the_request(monkeypatch):
    import threading

    release = threading.Event()
    monkeypatch.setattr(network, "RESOLVE_TIMEOUT_S", 0.2)

    def slow(*args):
        release.wait(5)
        return []

    started = time.monotonic()
    try:
        assert (
            code(network.check_target, "slow.example.org", 5432, "d", "u", resolver=slow, internal=lambda: INTERNAL)
            == "host_unresolvable"
        )
        assert time.monotonic() - started < 2  # it did not wait for the resolver to finish
    finally:
        release.set()


# ---------- the application's own database ----------


def test_the_internal_database_cannot_be_registered_as_an_external_one():
    assert code(judge, "127.0.0.1", 5433, "rag", "someone", addresses=("127.0.0.1",)) == "internal_database"
    # same machine, any case
    assert code(judge, "localhost", 5433, "RAG", "x", addresses=("::1",)) == "internal_database"


def test_this_computers_own_network_address_is_the_same_machine_as_localhost(monkeypatch):
    """The application's database is configured as localhost; the computer's LAN address leads to it as well."""
    lan = "192.168.50.7"
    monkeypatch.setattr(network, "_on_this_machine", lambda a: a.is_loopback or str(a) == lan)
    assert code(judge, lan, 5433, "rag", "someone", addresses=(lan,)) == "internal_database"
    assert code(judge, lan, 5433, "other", "rag_app", addresses=(lan,)) == "internal_database"
    assert judge("192.168.50.8", 5433, "rag", "reader", addresses=("192.168.50.8",)).addresses  # another computer


def test_which_addresses_are_this_computers_own():
    import ipaddress

    assert network._on_this_machine(ipaddress.ip_address("127.0.0.1"))
    assert not network._on_this_machine(ipaddress.ip_address("192.0.2.1"))  # a documentation address: nobody's


def test_one_of_the_internal_roles_cannot_be_used_on_the_internal_server():
    for role in ("rag", "rag_app", "rag_migrator", "rag_bootstrap"):
        assert code(judge, "127.0.0.1", 5433, "other_db", role, addresses=("127.0.0.1",)) == "internal_database"


def test_another_database_on_the_same_server_or_the_same_name_elsewhere_is_allowed():
    assert judge("127.0.0.1", 5433, "project_two", "reader", addresses=("127.0.0.1",)).loopback
    assert judge("db.example.org", 5433, "rag", "reader", addresses=("93.184.216.34",)).addresses  # other machine
    assert judge("127.0.0.1", 5432, "rag", "reader", addresses=("127.0.0.1",)).loopback  # another port: another server


def test_the_internal_database_is_found_in_both_modes(monkeypatch):
    monkeypatch.setattr(config, "DB_HARDENED", False)
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://u:p@db.internal:6000/appdb")
    assert network.internal_database() == ("db.internal", 6000, "appdb")
    monkeypatch.setattr(config, "DB_HARDENED", True)
    monkeypatch.setattr(config, "DB_HOST", "10.0.0.2")
    monkeypatch.setattr(config, "DB_PORT", 5999)
    monkeypatch.setattr(config, "DB_NAME", "hardened")
    assert network.internal_database() == ("10.0.0.2", 5999, "hardened")


# ---------- the optional allow-list ----------


def test_with_an_allow_list_nothing_else_may_be_connected_to(monkeypatch):
    monkeypatch.setattr(config, "CONNECTOR_ALLOWED_HOSTS", ("*.example.org", "localhost"))
    assert judge("db.example.org").addresses
    assert code(judge, "evil.test") == "host_not_allowed"
    assert code(judge, "example.org.evil.test") == "host_not_allowed"
    assert judge("localhost", addresses=("127.0.0.1",)).loopback


def test_without_an_allow_list_any_unblocked_host_is_fine(monkeypatch):
    monkeypatch.setattr(config, "CONNECTOR_ALLOWED_HOSTS", ())
    assert judge("anything.example.org").addresses


def test_a_ca_certificate_that_is_not_one_is_refused():
    pytest.importorskip("cryptography")  # (without it the answer is "driver_missing": nothing can be parsed)
    assert code(validation.connection_fields, {**GOOD, "options": {"ca_certificate_pem": "not a certificate"}}) == (
        "invalid_options"
    )
    assert code(validation.connection_fields, {**GOOD, "options": {"ca_certificate_pem": "x" * 70000}}) == (
        "invalid_options"
    )


def test_without_the_cryptography_library_a_ca_certificate_cannot_be_checked_and_is_not_accepted_unchecked(monkeypatch):
    import builtins

    real = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name.startswith("cryptography"):
            raise ImportError(name)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    with pytest.raises(ConnectorError) as error:
        validation.connection_fields({**GOOD, "options": {"ca_certificate_pem": "-----BEGIN CERTIFICATE-----\nx"}})
    assert error.value.code == "driver_missing" and error.value.status == 503
