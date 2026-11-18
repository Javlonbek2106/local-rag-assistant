"""Roadmap M4.1a, decision D-14: the security event log writes only allowed events, keeps secrets out, rotates, and
never loses an event silently."""
import json
import os
import re

import pytest

from rag import config
from rag.auth import secretfiles
from rag.auth.events import EVENTS, FILE_NAME, SecurityLog

SENTINELS = ("hunter2-password", "123456789", "Bearer abc", "SELECT secret FROM t", "Traceback")
OWNER = "6f9619ff-8b86-d011-b42d-00c04fc964ff"
SESSION = "2c1d5f3e-1b6a-4c58-9d1e-0a1b2c3d4e5f"


@pytest.fixture
def log(tmp_path):
    directory = tmp_path / "logs"
    secretfiles.ensure_private_dir(directory)
    return SecurityLog(directory)


def lines(log):
    return [json.loads(x) for x in (log.directory / FILE_NAME).read_text(encoding="utf-8").splitlines()]


def test_an_event_is_one_json_line_with_fixed_fields(log):
    assert log.emit("session_created", "ok", principal_id=OWNER, event_ref=SESSION)
    [record] = lines(log)
    assert set(record) == {"ts", "event", "reason", "principal_id", "event_ref"}
    assert record["event"] == "session_created" and record["reason"] == "ok"
    assert record["principal_id"] == OWNER and record["event_ref"] == SESSION
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT[\d:.]+\+00:00", record["ts"])


def test_the_failed_checks_are_listed_by_name_only(log):
    log.emit("readiness_security_changed", "check_failed", failed=("db_role_unprivileged", "principal_ok"))
    assert lines(log)[0]["failed"] == ["db_role_unprivileged", "principal_ok"]


@pytest.mark.parametrize("event,reason", [("login_with_password", "ok"), ("session_created", "because I said so"),
                                          ("pairing_exchange_ok", "invalid"), ("session_revoked", SENTINELS[0])])
def test_an_event_or_reason_outside_the_allow_list_is_refused_and_nothing_is_written(log, event, reason):
    with pytest.raises(ValueError):
        log.emit(event, reason)
    assert not (log.directory / FILE_NAME).exists()


@pytest.mark.parametrize("bad", [dict(principal_id="not-a-uuid"), dict(event_ref=SENTINELS[1]),
                                 dict(failed=("a password",))])
def test_an_id_or_check_name_that_is_not_what_it_should_be_is_refused(log, bad):
    with pytest.raises(ValueError):
        log.emit("session_created", "ok", **bad)
    assert not (log.directory / FILE_NAME).exists()


def test_there_is_no_way_to_write_free_text():
    """The allow list is the only door: every reason is a short fixed word, none can hold a secret or a sentence."""
    words = {reason for reasons in EVENTS.values() for reason in reasons}
    assert all(re.fullmatch(r"[a-z_]{2,20}", word) for word in words)
    assert not [word for word in words for s in SENTINELS if s.lower() in word]


def test_the_file_is_private_to_this_user(log):
    log.emit("session_created", "ok")
    secretfiles.verify_private(log.directory / FILE_NAME)
    if os.name != "nt":
        assert oct((log.directory / FILE_NAME).stat().st_mode & 0o777) == "0o600"


def test_the_log_rotates_by_size_and_keeps_a_fixed_number_of_old_files(log, monkeypatch):
    monkeypatch.setattr(config, "SECURITY_LOG_MAX_BYTES", 400)
    monkeypatch.setattr(config, "SECURITY_LOG_BACKUPS", 2)
    for _ in range(40):
        assert log.emit("session_created", "ok", principal_id=OWNER)
    names = sorted(p.name for p in log.directory.iterdir())
    assert names == [FILE_NAME, f"{FILE_NAME}.1", f"{FILE_NAME}.2"]
    assert all(p.stat().st_size <= 400 for p in log.directory.iterdir())
    for p in log.directory.iterdir():
        secretfiles.verify_private(p)


def test_a_failed_write_is_reported_and_the_log_says_it_is_unhealthy_until_a_write_works(log, monkeypatch):
    assert log.emit("session_created", "ok") and log.check()
    real_open = os.open

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "open", broken)
    assert log.emit("session_created", "ok") is False  # the caller learns that the event is lost
    assert log.healthy is False and log.check() is False
    monkeypatch.setattr(os, "open", real_open)
    assert log.emit("session_created", "ok") is True
    assert log.healthy and log.check()


def test_a_directory_that_is_not_acceptable_is_a_failed_write_not_a_silent_one(tmp_path):
    outside = SecurityLog(config.ROOT / "no-security-log-in-the-project")  # inside the project: refused
    assert outside.emit("session_created", "ok") is False and outside.check() is False
    assert not (config.ROOT / "no-security-log-in-the-project").exists()
    missing = SecurityLog(tmp_path / "never-made")
    assert missing.emit("session_created", "ok") is False and missing.healthy is False


def test_the_events_log_is_not_a_logging_handler_so_the_console_never_sees_it(log, caplog):
    with caplog.at_level("DEBUG"):
        log.emit("session_created", "ok", principal_id=OWNER)
    assert caplog.records == []
