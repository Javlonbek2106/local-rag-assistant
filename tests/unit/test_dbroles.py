"""Roadmap M4.1a, step 7: the roles, passwords and permissions of the hardened PostgreSQL set-up (no server needed;
tests/integration/test_pg_hardening.py runs the same code against a real one)."""
import base64
import hashlib
import hmac
import re
import subprocess

import psycopg
import pytest

from rag import config, db, dbroles
from rag.auth import checks, secretfiles
from rag.auth.secretfiles import SecretError
from rag.target import Target

STRONG = "Zx9Qk2vLm4Rt7Yp1Wc3Nb6Hd8Fg5Js0A"
SALT = b"0123456789abcdef"


@pytest.fixture
def secrets_dir(tmp_path, monkeypatch):
    directory = tmp_path / "secrets"
    monkeypatch.setattr(config, "SECRETS_DIR", directory)
    monkeypatch.setattr(config, "SECURITY_LOG_DIR", tmp_path / "security-log")  # never the real log of this computer
    return directory


# ---------- the SCRAM verifier ----------

def parts(verifier):
    match = re.fullmatch(r"SCRAM-SHA-256\$(\d+):([A-Za-z0-9+/=]+)\$([A-Za-z0-9+/=]+):([A-Za-z0-9+/=]+)", verifier)
    assert match, verifier
    return int(match.group(1)), *(base64.b64decode(match.group(i)) for i in (2, 3, 4))


def test_a_verifier_has_the_format_postgresql_stores():
    iterations, salt, stored, server = parts(dbroles.scram_verifier(STRONG, salt=SALT))
    assert iterations == 4096 and salt == SALT and len(stored) == 32 and len(server) == 32


def test_a_verifier_is_what_rfc_5802_says_for_the_password():
    salted = hashlib.pbkdf2_hmac("sha256", STRONG.encode(), SALT, 4096)
    client_key = hmac.new(salted, b"Client Key", "sha256").digest()
    _, _, stored, server = parts(dbroles.scram_verifier(STRONG, salt=SALT))
    assert stored == hashlib.sha256(client_key).digest()
    assert server == hmac.new(salted, b"Server Key", "sha256").digest()


def test_a_verifier_does_not_contain_the_password_and_differs_every_time():
    first, second = dbroles.scram_verifier(STRONG), dbroles.scram_verifier(STRONG)
    assert STRONG not in first and first != second


# ---------- passwords ----------

def test_a_new_password_is_long_random_and_strong():
    first, second = dbroles.new_password(), dbroles.new_password()
    assert first != second and dbroles.password_is_strong(first) and len(first) >= 43


@pytest.mark.parametrize("weak", ["", "rag", "postgres", "password", "short-one", "a" * 30, "ab" * 15])
def test_a_weak_or_default_password_is_refused(weak):
    assert not dbroles.password_is_strong(weak)


def test_the_secret_files_are_made_once_and_never_overwritten(secrets_dir):
    assert set(dbroles.ensure_secrets()) == set(dbroles.ROLES)
    before = {role: dbroles.read_password(role) for role in dbroles.ROLES}
    assert dbroles.ensure_secrets() == []
    assert {role: dbroles.read_password(role) for role in dbroles.ROLES} == before
    assert len(set(before.values())) == 3 and all(dbroles.password_is_strong(v) for v in before.values())
    assert dbroles.passwords() == before


def test_set_up_refuses_a_weak_password_in_a_file(secrets_dir):
    dbroles.ensure_secrets()
    secretfiles.write_secret(dbroles.SECRET_FILES[dbroles.APP], "rag\n")
    with pytest.raises(dbroles.DbSetupError):
        dbroles.passwords()


def test_set_up_refuses_one_password_for_two_roles(secrets_dir):
    dbroles.ensure_secrets()
    secretfiles.write_secret(dbroles.SECRET_FILES[dbroles.APP], dbroles.read_password(dbroles.MIGRATOR) + "\n")
    with pytest.raises(dbroles.DbSetupError):
        dbroles.passwords()


def test_a_missing_password_file_is_reported_not_replaced_by_a_default(secrets_dir):
    with pytest.raises(SecretError) as error:
        dbroles.read_password(dbroles.APP)
    assert error.value.code == "missing"


# ---------- nothing sensitive in a command line, a statement or a message ----------

def test_the_connection_settings_never_hold_a_password(monkeypatch):
    monkeypatch.setattr(config, "DB_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "DB_PORT", 5433)
    text = dbroles.conninfo(dbroles.APP)
    assert "password" not in text and "user=rag_app" in text and "port=5433" in text


def test_the_set_up_script_has_verifiers_but_no_clear_password():
    found = {role: dbroles.new_password() for role in dbroles.ROLES}
    sql = dbroles.setup_sql(found, "public", legacy_present=True)
    assert sql.count("SCRAM-SHA-256$") == 3 and not any(password in sql for password in found.values())
    assert sql.count(dbroles.NO_ATTRIBUTES) == 4  # two roles, each made and repaired
    assert "ALTER ROLE rag NOLOGIN PASSWORD NULL" in sql
    assert "ALTER ROLE rag NOLOGIN" not in dbroles.setup_sql(found, "public", legacy_present=False)


def test_psql_gets_the_statements_on_standard_input_and_nothing_secret_in_the_arguments(monkeypatch):
    seen = {}

    def fake_run(command, **kwargs):
        seen.update(command=command, **kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=b"1\n", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    sql = f"ALTER ROLE rag_app PASSWORD '{dbroles.scram_verifier(STRONG)}';"
    dbroles.psql(sql, container="c1")
    assert "SCRAM" not in " ".join(seen["command"]) and sql.encode() == seen["input"]
    assert seen["command"][:4] == ["docker", "exec", "-i", "c1"]


def test_a_psql_error_does_not_echo_the_statement(monkeypatch):
    stderr = b"ERROR: syntax error\nLINE 1: ALTER ROLE x PASSWORD 'SCRAM-SHA-256$4096:aa$bb:cc'\n"
    monkeypatch.setattr(subprocess, "run", lambda c, **k: subprocess.CompletedProcess(c, 3, b"", stderr))
    with pytest.raises(dbroles.DbSetupError) as error:
        dbroles.psql("x", container="c1")
    assert "SCRAM" not in str(error.value) and "syntax error" in str(error.value)


def test_a_failed_rotation_keeps_the_old_password_file_and_leaves_nothing_behind(secrets_dir, monkeypatch):
    dbroles.ensure_secrets()
    old = dbroles.read_password(dbroles.APP)

    def refuse(*args, **kwargs):
        raise dbroles.DbSetupError("psql failed")

    monkeypatch.setattr(dbroles, "psql", refuse)
    monkeypatch.setattr(dbroles, "superuser", lambda container=None: "rag_bootstrap")
    with pytest.raises(dbroles.DbSetupError):
        dbroles.rotate(dbroles.APP)
    assert dbroles.read_password(dbroles.APP) == old
    assert not list(secrets_dir.glob("*.new"))


def test_a_rotation_changes_the_file_only_after_the_server_accepted(secrets_dir, monkeypatch):
    dbroles.ensure_secrets()
    old, sent = dbroles.read_password(dbroles.APP), []
    monkeypatch.setattr(dbroles, "psql", lambda sql, container=None, user=None, database=None: sent.append(sql) or "")
    monkeypatch.setattr(dbroles, "superuser", lambda container=None: "rag_bootstrap")
    dbroles.rotate(dbroles.APP)
    new = dbroles.read_password(dbroles.APP)
    assert new != old and dbroles.password_is_strong(new) and new not in sent[0] and "rag_app" in sent[0]
    assert not list(secrets_dir.glob("*.new"))


# ---------- the runtime pool ----------

def test_the_runtime_pool_will_not_open_without_its_secret(secrets_dir, monkeypatch):
    monkeypatch.setattr(config, "DB_HARDENED", True)
    db.pool.cache_clear()
    with pytest.raises(SecretError):
        db.pool()
    assert db.pool.cache_info().currsize == 0


def test_every_new_runtime_connection_reads_the_password_again(secrets_dir, monkeypatch):
    dbroles.ensure_secrets()
    used = []
    monkeypatch.setattr(psycopg.Connection, "connect",
                        classmethod(lambda cls, conninfo="", **kw: used.append(kw["password"])))
    db._RuntimeConnection.connect("host=x")
    secretfiles.write_secret(dbroles.SECRET_FILES[dbroles.APP], "A-newly-rotated-password-0123\n")
    db._RuntimeConnection.connect("host=x")
    assert used[1] == "A-newly-rotated-password-0123" and used[0] != used[1]


def test_the_backup_url_carries_the_password_only_in_memory(secrets_dir, monkeypatch):
    dbroles.ensure_secrets()
    monkeypatch.setattr(config, "DB_HARDENED", True)
    password = dbroles.read_password(dbroles.APP)
    assert password in db.runtime_url() and "rag_app" in db.runtime_url()
    assert password not in str(Target.from_config().describe())


def test_without_the_hardened_mode_the_url_is_the_old_one(monkeypatch):
    monkeypatch.setattr(config, "DB_HARDENED", False)
    assert db.runtime_url() == config.DATABASE_URL


# ---------- permissions are decided table by table ----------

def test_every_table_the_migrations_create_has_its_runtime_grant_decided():
    made = {"schema_migrations"}
    for path in db.MIGRATIONS.glob("*.sql"):
        made |= set(re.findall(r"CREATE TABLE (?:IF NOT EXISTS )?([a-z_]+)", path.read_text(encoding="utf-8")))
    assert made == set(dbroles.RUNTIME_GRANTS), "a migration added a table: decide what the application may do with it"


def test_the_application_cannot_change_the_record_of_applied_migrations_or_the_principals():
    assert dbroles.RUNTIME_GRANTS["schema_migrations"] == ("SELECT",)
    assert dbroles.RUNTIME_GRANTS["principals"] == ("SELECT",)
    assert not {"TRUNCATE", "REFERENCES", "TRIGGER"} & {p for v in dbroles.RUNTIME_GRANTS.values() for p in v}


# ---------- the connector's view of this ----------

def test_the_connector_check_for_runtime_permissions_fails_without_the_hardened_mode(monkeypatch):
    monkeypatch.setattr(config, "DB_HARDENED", False)
    assert checks.db_runtime_permissions() is False


def test_the_password_check_in_hardened_mode_reads_the_files_not_the_url(secrets_dir, monkeypatch):
    monkeypatch.setattr(config, "DB_HARDENED", True)
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://rag:rag@127.0.0.1:5433/rag")  # weak, but not used now
    assert checks.db_password_not_default() is False  # no files yet
    dbroles.ensure_secrets()
    assert checks.db_password_not_default() is True
    secretfiles.write_secret(dbroles.SECRET_FILES[dbroles.APP], "postgres\n")
    assert checks.db_password_not_default() is False
