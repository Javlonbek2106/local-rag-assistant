"""M4.1b: the vault tools (key-init, key-rotate, rewrap, verify) against a real database and a real key file."""

import json

import pytest

pytest.importorskip("cryptography")

from rag import config, connectorcli  # noqa: E402
from rag.auth import secretfiles  # noqa: E402
from rag.auth.events import SecurityLog  # noqa: E402
from rag.auth.principals import Principals  # noqa: E402
from rag.connector import repository  # noqa: E402
from rag.connector.service import Service  # noqa: E402
from rag.connector.vault import Vault, new_key_line, parse_keyring  # noqa: E402
from rag.connector_wiring import read_key_ring  # noqa: E402
from tests.auth_support import Events  # noqa: E402
from tests.connector_support import PASSWORD, PAYLOAD, FakeDriver, internal, resolver  # noqa: E402
from tests.db.test_connector_service import admin  # noqa: E402

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def clean(database):
    with admin() as conn:
        conn.execute("DELETE FROM connections")
    yield
    with admin() as conn:
        conn.execute("DELETE FROM connections")


@pytest.fixture
def secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SECRETS_DIR", tmp_path / "secrets")
    monkeypatch.setattr(config, "SECURITY_LOG_DIR", tmp_path / "log")
    return tmp_path


@pytest.fixture
def events(secrets):
    return SecurityLog()


@pytest.fixture
def service():
    return Service(Vault(read_key_ring), Events(), resolver=resolver(), internal=internal, driver_lookup=FakeDriver())


@pytest.fixture
def owner():
    return Principals().local_owner()["id"]


def logged(secrets):
    return [json.loads(line) for line in (secrets / "log" / "events.log").read_text(encoding="utf-8").splitlines()]


def key_ids():
    return [line.partition(":")[0] for line in read_key_ring().splitlines() if line.strip()]


def test_the_key_is_made_once_private_and_recorded_and_never_replaced(secrets, events, capsys):
    assert connectorcli.key_init(events) == 0
    first = read_key_ring()
    secretfiles.verify_private(config.SECRETS_DIR)
    secretfiles.verify_private(config.SECRETS_DIR / config.ENCRYPTION_KEY_FILE)
    assert parse_keyring(first)[0] and [e["event"] for e in logged(secrets)] == ["key_created"]
    assert connectorcli.key_init(events) == 0 and read_key_ring() == first  # a second time changes nothing
    assert len(logged(secrets)) == 1
    printed = capsys.readouterr()
    assert first.partition(":")[2] not in printed.out + printed.err  # the key is never printed


def test_a_damaged_key_file_is_not_replaced(secrets, events, capsys):
    secretfiles.write_secret(config.ENCRYPTION_KEY_FILE, "this is not a key ring\n")
    assert connectorcli.key_init(events) == 1
    assert read_key_ring() == "this is not a key ring"  # still there: the operator decides what to do with it
    assert "NOT replaced" in capsys.readouterr().err


def test_a_key_is_not_made_when_the_security_log_cannot_be_written(secrets, monkeypatch, capsys):
    monkeypatch.setattr(config, "SECURITY_LOG_DIR", config.ROOT / "log-inside-the-project")  # not an acceptable place
    assert connectorcli.key_init(SecurityLog()) == 1
    with pytest.raises(secretfiles.SecretError) as missing:
        read_key_ring()
    assert missing.value.code == "missing" and "refusing" in capsys.readouterr().err


def test_rotation_adds_a_key_on_top_and_rewrap_moves_every_credential_to_it(secrets, events, service, owner, capsys):
    connectorcli.key_init(events)
    old_id = key_ids()[0]
    made = [service.create(owner, {**PAYLOAD, "name": f"c{i}"}) for i in range(3)]
    assert connectorcli.key_rotate(events) == 0
    new_id = key_ids()[0]
    assert new_id != old_id and key_ids() == [new_id, old_id]
    assert {repository.get_sealed(owner, m["id"]).key_id for m in made} == {old_id}
    assert connectorcli.verify(Vault(read_key_ring)) == 0  # the old keys still open everything
    assert connectorcli.rewrap(Vault(read_key_ring), events) == 0
    assert {repository.get_sealed(owner, m["id"]).key_id for m in made} == {new_id}
    out = capsys.readouterr().out
    assert "re-encrypted: 3" in out
    assert (
        connectorcli.rewrap(Vault(read_key_ring), events) == 0
        and "re-encrypted: 0; already under the active key: 3" in capsys.readouterr().out
    )  # safe to repeat
    assert Vault(read_key_ring).open(repository.get_sealed(owner, made[0]["id"]), made[0]["id"]) == PASSWORD
    assert [e["event"] for e in logged(secrets)] == ["key_created", "key_created", "key_rewrapped"]


def test_after_a_rewrap_the_old_key_can_be_removed_and_everything_still_opens(secrets, events, service, owner):
    connectorcli.key_init(events)
    made = service.create(owner, PAYLOAD)
    connectorcli.key_rotate(events)
    connectorcli.rewrap(Vault(read_key_ring), events)
    newest = read_key_ring().splitlines()[0]
    secretfiles.write_secret(config.ENCRYPTION_KEY_FILE, newest + "\n")  # the old key is retired
    assert connectorcli.verify(Vault(read_key_ring)) == 0
    assert service.get(owner, made["id"])["status"] == "ready"


def test_verify_reports_what_does_not_open_by_count_and_code_and_never_by_value(
    secrets, events, service, owner, capsys
):
    connectorcli.key_init(events)
    service.create(owner, {**PAYLOAD, "name": "a"})
    service.create(owner, {**PAYLOAD, "name": "b"})
    capsys.readouterr()
    assert connectorcli.verify(Vault(read_key_ring)) == 0
    assert "credentials that open: 2; that do not: 0" in capsys.readouterr().out
    secretfiles.write_secret(config.ENCRYPTION_KEY_FILE, new_key_line("other") + "\n")  # a different key
    assert connectorcli.verify(Vault(read_key_ring)) == 1
    out = capsys.readouterr().out
    assert "credentials that open: 0; that do not: 2 (unknown_key: 2)" in out and PASSWORD not in out


def test_verify_finds_a_ciphertext_that_was_tampered_with(secrets, events, service, owner, capsys):
    connectorcli.key_init(events)
    made = service.create(owner, PAYLOAD)
    with admin() as conn:
        conn.execute(
            "UPDATE connections SET credential_ciphertext = set_byte(credential_ciphertext, 0, "
            "get_byte(credential_ciphertext, 0) # 1) WHERE id = %s",
            (made["id"],),
        )
    capsys.readouterr()
    assert connectorcli.verify(Vault(read_key_ring)) == 1 and "decrypt_failed: 1" in capsys.readouterr().out


def test_rewrap_stops_at_a_credential_it_cannot_open_and_changes_nothing_more(secrets, events, service, owner, capsys):
    connectorcli.key_init(events)
    good, bad = service.create(owner, {**PAYLOAD, "name": "a"}), service.create(owner, {**PAYLOAD, "name": "b"})
    connectorcli.key_rotate(events)
    with admin() as conn:
        conn.execute(
            "UPDATE connections SET credential_ciphertext = set_byte(credential_ciphertext, 0, "
            "get_byte(credential_ciphertext, 0) # 1) WHERE id = %s",
            (bad["id"],),
        )
    capsys.readouterr()
    assert connectorcli.rewrap(Vault(read_key_ring), events) == 1
    assert "could not be opened" in capsys.readouterr().err
    assert Vault(read_key_ring).open(repository.get_sealed(owner, good["id"]), good["id"]) == PASSWORD  # still opens


def test_a_rotation_or_rewrap_is_refused_when_the_log_cannot_be_written(secrets, events, service, owner, monkeypatch):
    connectorcli.key_init(events)
    made = service.create(owner, PAYLOAD)
    before = read_key_ring()
    monkeypatch.setattr(config, "SECURITY_LOG_DIR", config.ROOT / "log-inside-the-project")
    broken = SecurityLog()
    assert connectorcli.key_rotate(broken) != 0 and read_key_ring() == before
    assert connectorcli.rewrap(Vault(read_key_ring), broken) != 0
    assert repository.get_sealed(owner, made["id"]).key_id == key_ids()[0]


def test_the_tools_need_a_key_ring_to_rotate_or_rewrap(secrets, events, capsys):
    assert connectorcli.key_rotate(events) == 1 and "key-init" in capsys.readouterr().err
    assert connectorcli.rewrap(Vault(read_key_ring), events) == 1 and "key_missing" in capsys.readouterr().err


def test_the_command_line_entry_point_runs_the_same_functions(secrets, capsys):
    assert connectorcli.main(["key-init"]) == 0
    assert connectorcli.main(["verify"]) == 0 and "credentials that open: 0; that do not: 0" in capsys.readouterr().out
