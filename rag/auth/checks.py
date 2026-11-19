"""The security conditions the connector needs, checked at start-up and again whenever readiness is asked (D-7, D-15).

Each check answers True or False and nothing else: no value, path or error text leaves this module. A check that cannot
be run (the database is down, a file cannot be read) is False: unknown is never "fine". The result is reused for
SECURITY_CHECK_TTL_S so that a busy readiness probe does not hammer the database; a change is written to the security
log (the names of the checks that failed, never why).

They matter only when the connector is enabled; with the connector off none of this runs (D-15).
"""
import logging
import threading
import time

from psycopg.conninfo import conninfo_to_dict

from rag import config, db, dbroles
from rag.auth import secretfiles
from rag.auth.events import CHECK_NAMES

log = logging.getLogger(__name__)
KEY_FILE = "encryption.key"  # where the vault key will live (a later milestone): only its place is held to the rule
WEAK_PASSWORDS, MIN_PASSWORD_LENGTH = dbroles.WEAK_PASSWORDS, dbroles.MIN_PASSWORD_LENGTH
BROAD_ROLES = ("pg_execute_server_program", "pg_read_server_files", "pg_write_server_files", "pg_read_all_data",
               "pg_write_all_data")
ROLE_SQL = ("SELECT r.rolsuper, r.rolcreaterole, r.rolcreatedb, r.rolreplication, r.rolbypassrls, "
            + " OR ".join(f"pg_has_role(current_user, '{role}', 'member')" for role in BROAD_ROLES) + " AS broad "
            "FROM pg_roles r WHERE r.rolname = current_user")


def db_role_unprivileged():
    """The role the application connects as is no superuser, cannot make roles or databases, replicate or bypass row
    security, and is not a member of the roles that read or write server files, run programs or read everything."""
    with db.pool().connection(timeout=config.SECURITY_DB_TIMEOUT_S) as conn:
        row = conn.execute(ROLE_SQL).fetchone()
    return row is not None and not any(row.values())


RUNTIME_SQL = ("SELECT current_user AS who, "
               "has_schema_privilege(current_user, current_schema(), 'CREATE') AS creates_objects, "
               "has_database_privilege(current_user, current_database(), 'CREATE') AS creates_schemas, "
               "has_database_privilege(current_user, current_database(), 'TEMPORARY') AS creates_temp, "
               "EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
               "WHERE n.nspname = current_schema() AND pg_get_userbyid(c.relowner) = current_user) AS owns_objects, "
               "has_table_privilege(current_user, 'schema_migrations', 'INSERT, UPDATE, DELETE, TRUNCATE') "
               "AS edits_migrations")


def db_runtime_permissions():
    """Hardened set-up only: the application runs as rag_app (not as the migrator or the bootstrap superuser), and that
    role can change no schema, owns nothing, and cannot touch the record of applied migrations. Single-role mode
    cannot say this, so it fails here (the connector needs the three-role set-up)."""
    if not config.DB_HARDENED:
        return False
    with db.pool().connection(timeout=config.SECURITY_DB_TIMEOUT_S) as conn:
        row = conn.execute(RUNTIME_SQL).fetchone()
    return row["who"] == dbroles.APP and not any(value for key, value in row.items() if key != "who")


def db_password_not_default():
    if config.DB_HARDENED:  # three secret files, each strong, none the same as another
        try:
            found = dbroles.passwords()
        except (dbroles.DbSetupError, secretfiles.SecretError):
            return False
        return len(found) == len(dbroles.ROLES)
    password = conninfo_to_dict(config.DATABASE_URL).get("password") or ""
    return dbroles.password_is_strong(password)


def secrets_dir_private():
    secretfiles.verify_private(config.SECRETS_DIR)
    return True


def key_location_ok():
    """Held to the rule of D-8: no key may be in the project, the database or a place others can read. Not yet being
    there is fine (the vault comes later); being there in a bad place is not."""
    key = config.SECRETS_DIR / KEY_FILE
    if key.exists():
        secretfiles.verify_private(config.SECRETS_DIR)
        secretfiles.verify_private(key)
    return True


def default_checks(principals, events):
    return {"principal_ok": lambda: bool(principals.local_owner()),
            "db_role_unprivileged": db_role_unprivileged,
            "db_runtime_permissions": db_runtime_permissions,
            "db_password_not_default": db_password_not_default,
            "secrets_dir_private": secrets_dir_private,
            "key_location_ok": key_location_ok,
            "security_log_writable": events.check}


class SecurityChecks:
    def __init__(self, checks, events, clock=time.monotonic):
        assert set(checks) == CHECK_NAMES
        self._checks, self._events, self._clock = checks, events, clock
        self._lock = threading.Lock()
        self._result, self._at = None, 0.0

    def _run(self):
        result = {}
        for name, check in self._checks.items():
            try:
                result[name] = bool(check())
            except Exception:  # noqa: BLE001 - unknown is not fine, and the reason is not for the caller
                result[name] = False
        return result

    def snapshot(self, force=False):
        """{check name: bool}; the last result while it is younger than SECURITY_CHECK_TTL_S."""
        with self._lock:
            now = self._clock()
            if not force and self._result is not None and now - self._at < config.SECURITY_CHECK_TTL_S:
                return dict(self._result)
            before = self._result
            self._result, self._at = self._run(), now
            if before is not None and before != self._result:
                failed = [name for name, ok in self._result.items() if not ok]
                self._events.emit("readiness_security_changed", "check_failed" if failed else "ok", failed=failed)
                if failed:
                    log.warning("connector unavailable, failed security checks: %s", ", ".join(sorted(failed)))
            return dict(self._result)

    def ok(self):
        return all(self.snapshot().values())
