"""Acceptance audit of M4.1a, area 5 and 4: every way a secret or the security log can go wrong ends in a refusal,
never in a guess, and never in a silent success. Each test runs where the system can do the thing it tests."""
import json
import os

import pytest

from rag import config, dbroles
from rag.auth import secretfiles
from rag.auth.events import SecurityLog
from rag.auth.secretfiles import SecretError

posix_only = pytest.mark.skipif(os.name == "nt", reason="uses POSIX permissions and owners")
STRONG = "Zx9Qk2vLm4Rt7Yp1Wc3Nb6Hd8Fg5Js0A"


@pytest.fixture
def secrets_dir(tmp_path, monkeypatch):
    directory = tmp_path / "secrets"
    monkeypatch.setattr(config, "SECRETS_DIR", directory)
    monkeypatch.setattr(config, "SECURITY_LOG_DIR", tmp_path / "security-log")
    return directory


def make_link(link, target):
    try:
        os.symlink(target, link, target_is_directory=os.path.isdir(target))
    except OSError:
        pytest.skip("this account may not create symbolic links")


# ---------- secret files ----------

def test_a_secret_that_is_a_link_is_refused_even_when_it_points_at_a_private_file(secrets_dir, tmp_path):
    secretfiles.write_secret("real", STRONG, secrets_dir)
    make_link(secrets_dir / "link", secrets_dir / "real")
    with pytest.raises(SecretError) as error:
        secretfiles.read_secret("link", secrets_dir)
    assert error.value.code == "symlink"


@pytest.mark.skipif(os.name != "nt", reason="junctions are a Windows thing")
def test_windows_a_secret_directory_that_is_a_junction_is_refused(tmp_path):
    import subprocess
    real = tmp_path / "real-secrets"
    secretfiles.write_secret("db_app_password", STRONG, real)
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(tmp_path / "junction"), str(real)], capture_output=True)
    assert made.returncode == 0
    with pytest.raises(SecretError) as error:
        secretfiles.read_secret("db_app_password", tmp_path / "junction")
    assert error.value.code == "symlink"


def test_a_secret_directory_that_is_a_link_is_refused(tmp_path):
    real = tmp_path / "real-secrets"
    secretfiles.write_secret("db_app_password", STRONG, real)
    make_link(tmp_path / "linked-secrets", real)
    with pytest.raises(SecretError) as error:
        secretfiles.read_secret("db_app_password", tmp_path / "linked-secrets")
    assert error.value.code == "symlink"


def test_a_leftover_temporary_file_or_link_is_never_followed_when_a_secret_is_written(secrets_dir, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched", encoding="utf-8")
    secretfiles.ensure_private_dir(secrets_dir)
    make_link(secrets_dir / "db_app_password.tmp", victim)  # what somebody might leave to redirect the write
    secretfiles.write_secret("db_app_password", STRONG, secrets_dir)
    assert victim.read_text(encoding="utf-8") == "untouched"
    assert secretfiles.read_secret("db_app_password", secrets_dir) == STRONG


def test_a_secret_that_is_not_text_is_unreadable_not_a_crash(secrets_dir):
    secretfiles.write_secret("db_app_password", "x", secrets_dir)
    (secrets_dir / "db_app_password").write_bytes(b"\xff\xfe\x00garbage\x80")
    with pytest.raises(SecretError) as error:
        secretfiles.read_secret("db_app_password", secrets_dir)
    assert error.value.code == "unreadable"


@pytest.mark.parametrize("content", ["two words here-making-it-long-enough\n",
                                     "line-one-is-long-enough-1234\nline-two\n",
                                     "tab\tinside-a-long-enough-password-1\n", "\x00" + "A" * 30])
def test_a_corrupted_password_file_is_refused_by_set_up_and_the_connector_check(secrets_dir, content, monkeypatch):
    from rag.auth import checks
    dbroles.ensure_secrets()
    secretfiles.write_secret(dbroles.SECRET_FILES[dbroles.APP], content, secrets_dir)
    with pytest.raises(dbroles.DbSetupError):
        dbroles.passwords()
    monkeypatch.setattr(config, "DB_HARDENED", True)
    assert checks.db_password_not_default() is False


def test_a_missing_secret_stays_missing_and_nothing_is_invented(secrets_dir):
    for role in dbroles.ROLES:
        with pytest.raises(SecretError) as error:
            dbroles.read_password(role)
        assert error.value.code == "missing"
    assert not secrets_dir.exists()  # reading never creates anything


@posix_only
def test_posix_a_secret_file_whose_permissions_were_loosened_is_refused_at_once(secrets_dir):
    secretfiles.write_secret("db_app_password", STRONG, secrets_dir)
    (secrets_dir / "db_app_password").chmod(0o644)
    with pytest.raises(SecretError) as error:
        secretfiles.read_secret("db_app_password", secrets_dir)
    assert error.value.code == "not_private"


@posix_only
def test_posix_a_secret_owned_by_somebody_else_is_refused(secrets_dir, monkeypatch):
    secretfiles.write_secret("db_app_password", STRONG, secrets_dir)
    monkeypatch.setattr(os, "getuid", lambda: os.stat(secrets_dir).st_uid + 1)
    with pytest.raises(SecretError) as error:
        secretfiles.read_secret("db_app_password", secrets_dir)
    assert error.value.code == "not_owner"


@posix_only
def test_posix_a_file_made_by_the_atomic_write_is_private_from_its_first_byte(secrets_dir, monkeypatch):
    seen = []
    real_replace = os.replace

    def spy(source, destination):
        seen.append(oct(os.stat(source).st_mode & 0o777))
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", spy)
    secretfiles.write_secret("db_app_password", STRONG, secrets_dir)
    assert seen == ["0o600"]


# ---------- the security log around a security change ----------

def read_events(directory):
    return [json.loads(line) for line in (directory / "events.log").read_text(encoding="utf-8").splitlines()]


def test_creating_the_password_files_is_recorded_without_a_value(secrets_dir, tmp_path):
    dbroles.ensure_secrets()
    (event,) = read_events(tmp_path / "security-log")
    assert event["event"] == "secrets_created" and event["reason"] == "ok"
    text = (tmp_path / "security-log" / "events.log").read_text(encoding="utf-8")
    assert all(dbroles.read_password(role) not in text for role in dbroles.ROLES)
    dbroles.ensure_secrets()  # nothing was created the second time: nothing is recorded
    assert len(read_events(tmp_path / "security-log")) == 1


def test_a_credential_is_not_created_when_its_record_cannot_be_kept(secrets_dir, monkeypatch):
    monkeypatch.setattr(config, "SECURITY_LOG_DIR", config.ROOT / "logs-inside-the-project")  # not an acceptable place
    with pytest.raises(dbroles.DbSetupError):
        dbroles.ensure_secrets()
    assert not secrets_dir.exists()


def test_a_rotation_is_recorded_with_the_role_only(secrets_dir, tmp_path, monkeypatch):
    dbroles.ensure_secrets()
    monkeypatch.setattr(dbroles, "psql", lambda *a, **k: "")
    monkeypatch.setattr(dbroles, "superuser", lambda container=None: "rag_bootstrap")
    for role in dbroles.ROLES:
        dbroles.rotate(role)
    events = read_events(tmp_path / "security-log")
    assert [(e["event"], e["reason"]) for e in events[1:]] == [
        ("credential_rotated", "bootstrap"), ("credential_rotated", "migrator"), ("credential_rotated", "app")]
    assert all(set(e) == {"ts", "event", "reason", "principal_id", "event_ref"} for e in events)


def test_a_rotation_is_refused_before_anything_changes_when_the_log_cannot_be_written(secrets_dir, monkeypatch):
    dbroles.ensure_secrets()
    before = dbroles.read_password(dbroles.APP)
    monkeypatch.setattr(SecurityLog, "check", lambda self: False)
    touched = []
    monkeypatch.setattr(dbroles, "psql", lambda *a, **k: touched.append(a) or "")
    with pytest.raises(dbroles.DbSetupError, match="refusing"):
        dbroles.rotate(dbroles.APP)
    assert touched == [] and dbroles.read_password(dbroles.APP) == before and not list(secrets_dir.glob("*.new"))


def test_a_rotation_that_could_not_be_recorded_afterwards_is_an_error_not_a_quiet_success(secrets_dir, monkeypatch):
    dbroles.ensure_secrets()
    monkeypatch.setattr(dbroles, "psql", lambda *a, **k: "")
    monkeypatch.setattr(dbroles, "superuser", lambda container=None: "rag_bootstrap")
    monkeypatch.setattr(SecurityLog, "emit", lambda self, *a, **k: False)
    with pytest.raises(dbroles.DbSetupError, match="NOT be written"):
        dbroles.rotate(dbroles.APP)


def test_a_failed_rotation_is_recorded_as_failed(secrets_dir, tmp_path, monkeypatch):
    dbroles.ensure_secrets()

    def refuse(*args, **kwargs):
        raise dbroles.DbSetupError("psql failed")

    monkeypatch.setattr(dbroles, "psql", refuse)
    monkeypatch.setattr(dbroles, "superuser", lambda container=None: "rag_bootstrap")
    with pytest.raises(dbroles.DbSetupError):
        dbroles.rotate(dbroles.MIGRATOR)
    assert [(e["event"], e["reason"]) for e in read_events(tmp_path / "security-log")][-1] == (
        "credential_rotation_failed", "migrator")


def fake_server(monkeypatch, legacy_open):
    """A psql that answers the questions set-up asks, and remembers the script it was asked to run."""
    ran = []

    def psql(sql, container=None, user=None, database=None):
        if "FROM pg_roles WHERE rolname = 'rag' AND rolsuper AND rolcanlogin" in sql:
            return "1\n" if legacy_open else ""
        if "FROM pg_roles WHERE rolname = 'rag' AND rolsuper" in sql:
            return "1\n"
        ran.append(sql)
        return ""

    monkeypatch.setattr(dbroles, "psql", psql)
    monkeypatch.setattr(dbroles, "superuser", lambda container=None: "rag")
    return ran


def test_an_old_database_is_not_touched_until_the_operator_says_so(secrets_dir, monkeypatch):
    dbroles.ensure_secrets()
    ran = fake_server(monkeypatch, legacy_open=True)
    with pytest.raises(dbroles.DbSetupError) as error:
        dbroles.setup(migrate=False)
    assert str(error.value) == dbroles.LEGACY_WARNING and "backup" in str(error.value) and ran == []


def test_with_the_operators_word_an_old_database_is_adopted_in_one_transaction_and_recorded(
        secrets_dir, tmp_path, monkeypatch):
    dbroles.ensure_secrets()
    ran = fake_server(monkeypatch, legacy_open=True)
    steps = dbroles.setup(adopt_legacy=True, migrate=False)
    assert [s[0] for s in steps] == ["roles", "legacy"]
    (script,) = ran
    assert script.startswith("BEGIN;") and script.rstrip().endswith("COMMIT;") and "ALTER ROLE rag NOLOGIN" in script
    assert [e["event"] for e in read_events(tmp_path / "security-log")][-2:] == ["legacy_role_closed", "db_setup"]


def test_set_up_that_cannot_be_recorded_does_not_start(secrets_dir, monkeypatch):
    dbroles.ensure_secrets()
    ran = fake_server(monkeypatch, legacy_open=False)
    monkeypatch.setattr(SecurityLog, "check", lambda self: False)
    with pytest.raises(dbroles.DbSetupError, match="refusing"):
        dbroles.setup(migrate=False)
    assert ran == []


def test_an_already_adopted_database_is_set_up_again_without_asking(secrets_dir, monkeypatch):
    dbroles.ensure_secrets()
    ran = fake_server(monkeypatch, legacy_open=False)  # `rag` is a superuser that can no longer log in
    assert [s[0] for s in dbroles.setup(migrate=False)] == ["roles", "legacy"] and len(ran) == 1


def test_the_first_event_of_a_flood_is_not_what_decides_whether_a_security_change_is_allowed(secrets_dir, tmp_path):
    log = SecurityLog(directory=tmp_path / "security-log")
    secretfiles.ensure_private_dir(tmp_path / "security-log")
    assert log.check() is True
    assert log.emit("db_setup", "ok") and log.check() is True
    with pytest.raises(ValueError):  # a value that is not on the list never reaches the file
        log.emit("db_setup", "password=hunter2")


def test_no_path_connects_with_a_weak_or_corrupted_password(secrets_dir, monkeypatch):
    from rag import db
    dbroles.ensure_secrets()
    monkeypatch.setattr(config, "DB_HARDENED", True)
    cases = (("postgres\n", "weak"), ("rag\n", "weak"), ("two words here-making-it-long-enough\n", "corrupted"))
    for bad, code in cases:
        for role in dbroles.ROLES:
            secretfiles.write_secret(dbroles.SECRET_FILES[role], bad, secrets_dir)
        db.close()
        with pytest.raises(SecretError) as error:  # the runtime pool
            db.pool()
        assert error.value.code == code
        with pytest.raises(SecretError):  # the migrator
            db._migrator_connection()
        with pytest.raises(SecretError):  # the backup tools' URL
            dbroles.url_with_password(dbroles.APP)
