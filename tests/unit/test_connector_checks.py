"""Roadmap M4.1a, decisions D-7 and D-15: the security checks, their cache, what they log, and the start-up."""
import logging

import pytest

from rag import config
from rag.auth import checks as checks_module
from rag.auth import secretfiles
from rag.auth.checks import SecurityChecks, default_checks
from rag.auth.deps import AuthContext
from rag.auth.events import CHECK_NAMES
from tests.auth_support import Clock, Events, FakePrincipals

STRONG = "Zx9Qk2vLm4Rt7Yp1Wc3Nb6"  # 22 characters
assert len(STRONG) == 22


def with_password(monkeypatch, password):
    userinfo = "rag" if password is None else f"rag:{password}"
    monkeypatch.setattr(config, "DATABASE_URL", f"postgresql://{userinfo}@127.0.0.1:5433/rag")


@pytest.mark.parametrize("password", ["rag", "postgres", "password", "PASSWORD", "changeme", "12345678", "short1",
                                      "a" * 40, "ababababababababababab", None])
def test_a_default_weak_short_or_missing_password_is_refused(monkeypatch, password):
    with_password(monkeypatch, password)
    assert checks_module.db_password_not_default() is False


def test_a_long_random_password_is_accepted(monkeypatch):
    with_password(monkeypatch, STRONG)
    assert checks_module.db_password_not_default() is True


def test_the_password_is_found_in_a_url_with_special_characters(monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://rag:Zx9Qk2vLm4Rt7Yp1Wc3Nb6%40%2F@127.0.0.1:5433/rag")
    assert checks_module.db_password_not_default() is True


def test_the_secrets_directory_must_be_private_and_outside_the_project(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SECRETS_DIR", tmp_path / "secrets")
    with pytest.raises(secretfiles.SecretError):  # not there yet
        checks_module.secrets_dir_private()
    secretfiles.ensure_private_dir(tmp_path / "secrets")
    assert checks_module.secrets_dir_private() is True
    monkeypatch.setattr(config, "SECRETS_DIR", config.ROOT / "somewhere")
    with pytest.raises(secretfiles.SecretError):
        checks_module.secrets_dir_private()


def test_the_key_may_be_absent_but_never_in_the_open(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SECRETS_DIR", tmp_path / "secrets")
    assert checks_module.key_location_ok() is True  # no key yet: the vault is a later milestone
    secretfiles.write_secret(checks_module.KEY_FILE, "key-material", tmp_path / "secrets")
    assert checks_module.key_location_ok() is True  # there, in a private place
    monkeypatch.setattr(config, "SECRETS_DIR", config.ROOT / "somewhere")
    (config.ROOT / "somewhere").mkdir(exist_ok=True)
    try:
        (config.ROOT / "somewhere" / checks_module.KEY_FILE).write_text("x")
        with pytest.raises(secretfiles.SecretError):
            checks_module.key_location_ok()  # a key inside the project is refused
    finally:
        (config.ROOT / "somewhere" / checks_module.KEY_FILE).unlink(missing_ok=True)
        (config.ROOT / "somewhere").rmdir()


def make(results, events=None, clock=None):
    """SecurityChecks whose checks answer from `results` (a dict a test can change) and count their calls."""
    calls = {name: 0 for name in CHECK_NAMES}

    def checker(name):
        def run():
            calls[name] += 1
            outcome = results[name]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return run

    events = events or Events()
    clock = clock or Clock()
    return SecurityChecks({name: checker(name) for name in CHECK_NAMES}, events, clock), calls, events, clock


def passing():
    return {name: True for name in CHECK_NAMES}


def test_all_checks_must_pass_and_the_set_of_names_is_fixed():
    checks, *_ = make(passing())
    assert checks.ok() is True
    with pytest.raises(AssertionError):
        SecurityChecks({"principal_ok": lambda: True}, Events())
    assert set(default_checks(FakePrincipals(), Events())) == CHECK_NAMES


def test_one_failing_check_makes_it_not_ok_and_unknown_is_not_fine():
    results = passing() | {"db_role_unprivileged": False}
    assert make(results)[0].ok() is False
    results = passing() | {"principal_ok": RuntimeError("postgresql://rag:secret-password@db/rag refused")}
    checks, *_ = make(results)
    assert checks.snapshot()["principal_ok"] is False and checks.ok() is False  # an error is a "no", not a crash


def test_the_result_is_reused_until_the_time_is_up(monkeypatch):
    checks, calls, _events, clock = make(passing())
    checks.ok()
    checks.ok()
    assert calls["principal_ok"] == 1
    clock.advance(config.SECURITY_CHECK_TTL_S - 1)
    checks.ok()
    assert calls["principal_ok"] == 1
    clock.advance(1)
    checks.ok()
    assert calls["principal_ok"] == 2
    checks.snapshot(force=True)
    assert calls["principal_ok"] == 3


def test_a_change_is_recorded_once_with_the_names_of_the_checks_only(caplog):
    results = passing()
    checks, _calls, events, clock = make(results)
    checks.snapshot()  # the first look is the start-up's business, not a change
    assert events.items == []
    results["db_password_not_default"] = False
    clock.advance(config.SECURITY_CHECK_TTL_S)
    with caplog.at_level(logging.WARNING):
        checks.snapshot()
        clock.advance(config.SECURITY_CHECK_TTL_S)
        checks.snapshot()  # still failing: not a new change
    assert events.names() == [("readiness_security_changed", "check_failed")]
    assert events.items[0]["failed"] == ["db_password_not_default"]
    assert "db_password_not_default" in caplog.text and "password=" not in caplog.text.lower()
    results["db_password_not_default"] = True
    clock.advance(config.SECURITY_CHECK_TTL_S)
    checks.snapshot()
    assert events.names()[-1] == ("readiness_security_changed", "ok")


def test_a_check_that_raises_leaks_nothing_into_the_log(caplog):
    results = passing() | {"principal_ok": RuntimeError("password=hunter2 in postgresql://u:hunter2@h/d")}
    checks, _calls, _events, clock = make(results)
    with caplog.at_level(logging.DEBUG):
        checks.snapshot()
        results["db_role_unprivileged"] = False
        clock.advance(config.SECURITY_CHECK_TTL_S)
        checks.snapshot()
    assert "hunter2" not in caplog.text


# ---------- start-up ----------

def test_startup_makes_the_private_directories_runs_the_checks_and_records_the_result(tmp_path):
    results = passing()
    checks, _calls, events, _clock = make(results)
    ctx = AuthContext(FakePrincipals(), events, checks=checks,
                      dirs=[tmp_path / "a" / "secrets", tmp_path / "b" / "log"])
    outcome = ctx.startup()
    assert all(outcome.values()) and events.names() == [("startup_security_check", "ok")]
    for name in ("a/secrets", "b/log"):
        secretfiles.verify_private(tmp_path / name)


def test_startup_survives_a_directory_it_cannot_accept_and_the_connector_stays_unavailable(caplog):
    results = passing() | {"secrets_dir_private": False}
    checks, _calls, events, _clock = make(results)
    ctx = AuthContext(FakePrincipals(), events, checks=checks, dirs=[config.ROOT / "inside-the-project"])
    with caplog.at_level(logging.WARNING):
        outcome = ctx.startup()  # does not raise
    assert not (config.ROOT / "inside-the-project").exists()  # nothing was made in the project
    assert outcome["secrets_dir_private"] is False and ctx.available() is False
    assert events.names() == [("startup_security_check", "check_failed")]
    assert "inside-the-project" not in caplog.text  # the log says that a directory is not available, not which one
