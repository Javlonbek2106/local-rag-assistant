"""Security events (decision D-14): what the authentication layer did, in a protected file of its own.

- Only the events listed in EVENTS can be written, each with one of its fixed reasons, and nothing else: no free text,
  so no secret, password, token, pairing code, key, SQL or raw exception can get into the file by accident.
- The file is outside the project, in a directory that must be private to this user (rag.auth.secretfiles). It is a
  writer of its own, not a logging handler: the ordinary logging of the application (console, INFO) never sees these
  events and they never see it.
- It is rotated by size and keeps a fixed number of old files (retention by count; a time limit is not decided yet).
- Writing can fail (disk full, directory no longer private). `emit` then returns False and the log reports itself as
  unhealthy until a write succeeds again, so a caller decides what a lost event means and nothing is lost silently.
"""
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from rag import config
from rag.auth import secretfiles

CHECK_NAMES = frozenset({"principal_ok", "db_role_unprivileged", "db_runtime_permissions", "db_password_not_default",
                         "secrets_dir_private", "key_location_ok", "security_log_writable"})
EVENTS = {
    "connection_created": {"ok"},
    "connection_updated": {"ok", "credential"},
    "connection_deleted": {"ok"},
    "connection_enabled": {"ok"},
    "connection_disabled": {"ok"},
    "connection_tested": {"ok", "failed"},
    "schema_discovered": {"ok", "failed"},
    "schema_selection_changed": {"ok"},
    "schema_changes_acknowledged": {"ok"},
    "sql_validated": {"ok", "rejected"},
    "sql_execution_started": {"ok"},
    "sql_executed": {"ok", "truncated", "timeout", "cancelled", "account_can_write", "permission_denied",
                    "query_error", "failed"},
    "key_created": {"ok"},
    "key_rewrapped": {"ok"},
    "db_setup": {"ok", "failed"},
    "legacy_role_closed": {"ok"},
    "secrets_created": {"ok"},
    "credential_rotated": {"app", "migrator", "bootstrap"},
    "credential_rotation_failed": {"app", "migrator", "bootstrap"},
    "pairing_requested": {"ok", "rate_limited", "unavailable"},
    "pairing_exchange_ok": {"ok"},
    "pairing_exchange_rejected": {"invalid", "rate_limited", "unavailable"},
    "session_created": {"ok"},
    "session_rotated": {"ok"},
    "session_revoked": {"logout", "revoke_all", "idle", "absolute", "evicted"},
    "step_up_ok": {"ok"},
    "step_up_rejected": {"invalid", "rate_limited"},
    "session_rejected": {"missing", "malformed", "idle", "absolute", "denied"},
    "readiness_security_changed": {"ok", "check_failed"},
    "startup_security_check": {"ok", "check_failed"},
}
FILE_NAME = "events.log"


def _uuid_or_none(value):
    return None if value is None else str(uuid.UUID(str(value)))


class SecurityLog:
    def __init__(self, directory=None, clock=time.time):
        self._directory = directory
        self._clock = clock
        self._lock = threading.Lock()
        self.healthy = True

    @property
    def directory(self):
        return Path(self._directory or config.SECURITY_LOG_DIR)

    def _record(self, event, reason, principal_id, event_ref, failed):
        if event not in EVENTS or reason not in EVENTS[event]:
            raise ValueError(f"not an allowed security event: {event}/{reason}")
        failed = sorted(failed)
        if not CHECK_NAMES.issuperset(failed):
            raise ValueError("not an allowed check name")
        record = {"ts": datetime.fromtimestamp(self._clock(), timezone.utc).isoformat(timespec="milliseconds"),
                  "event": event, "reason": reason, "principal_id": _uuid_or_none(principal_id),
                  "event_ref": _uuid_or_none(event_ref)}
        if failed:
            record["failed"] = failed
        return json.dumps(record, separators=(",", ":")) + "\n"

    def _rotate(self, path):
        backups = config.SECURITY_LOG_BACKUPS
        oldest = path.with_name(f"{FILE_NAME}.{backups}")
        oldest.unlink(missing_ok=True)
        for number in range(backups - 1, 0, -1):
            older = path.with_name(f"{FILE_NAME}.{number}")
            if older.exists():
                os.replace(older, path.with_name(f"{FILE_NAME}.{number + 1}"))
        os.replace(path, path.with_name(f"{FILE_NAME}.1"))

    def emit(self, event, reason, principal_id=None, event_ref=None, failed=()):
        """Write one event. True when it is on disk, False when it could not be written (the log is then unhealthy).
        A wrong event, reason or id is a programming error and raises ValueError before anything is written."""
        line = self._record(event, reason, principal_id, event_ref, failed)
        with self._lock:
            try:
                secretfiles.verify_private(self.directory)
                path = self.directory / FILE_NAME
                if path.exists() and path.stat().st_size + len(line) > config.SECURITY_LOG_MAX_BYTES:
                    self._rotate(path)
                descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(descriptor, "a", encoding="utf-8", newline="\n") as out:
                    out.write(line)
            except (OSError, secretfiles.SecretError):
                self.healthy = False
                return False
            self.healthy = True
            return True

    def check(self):
        """Can events be written right now: the directory is private, the file (if any) is writable, and the last write
        worked."""
        try:
            secretfiles.verify_private(self.directory)
            path = self.directory / FILE_NAME
            writable = os.access(self.directory, os.W_OK) and (not path.exists() or os.access(path, os.W_OK))
        except (OSError, secretfiles.SecretError):
            return False
        return bool(writable and self.healthy)
