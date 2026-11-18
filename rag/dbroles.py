"""PostgreSQL roles of the hardened set-up (roadmap M4.1a, step 7) and the tools that create and change them.

Three roles, three jobs, three passwords (never one of them in a URL, an argument, a log line or the repository):

- rag_bootstrap  superuser, only for creating and repairing the other two. It cannot log in over the network (the
                 server's pg_hba.conf rejects it) and the application never uses it: it is reached from inside the
                 database container (`docker exec`), which is as much as owning the Docker daemon already gives.
- rag_migrator   owns the database and every table; it is the only role that may run DDL. The application connects as
                 it only inside db.migrate(), only when something is pending, and closes the connection at once.
- rag_app        what the running application uses. No superuser, no CREATEDB / CREATEROLE / REPLICATION /
                 BYPASSRLS, no DDL, no role memberships; it may only do the data operations listed in RUNTIME_GRANTS.

Passwords live in files of the private secret directory (rag.auth.secretfiles). To set a password the clear text never
reaches the server: a SCRAM-SHA-256 verifier is computed here and that is what is sent.
"""
import base64
import hashlib
import hmac
import os
import re
import secrets
import subprocess
from pathlib import Path
from urllib.parse import quote

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from rag import config
from rag.auth import secretfiles
from rag.auth.events import SecurityLog
from rag.auth.secretfiles import SecretError

BOOTSTRAP, MIGRATOR, APP = "rag_bootstrap", "rag_migrator", "rag_app"
LEGACY = "rag"  # the single superuser the project used before (its password was "rag")
ROLES = (BOOTSTRAP, MIGRATOR, APP)
SECRET_FILES = {BOOTSTRAP: "db_bootstrap_password", MIGRATOR: "db_migrator_password", APP: "db_app_password"}
# Exactly what the running application may do, table by table. A table that is not listed gets nothing (fail closed):
# a migration that adds a table must add its line here (a test fails until it does).
RUNTIME_GRANTS = {"documents": ("SELECT", "INSERT", "UPDATE", "DELETE"),
                  "document_versions": ("SELECT", "INSERT", "UPDATE", "DELETE"),
                  "principals": ("SELECT",),
                  "schema_migrations": ("SELECT",)}
NO_ATTRIBUTES = "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
SCRAM_ITERATIONS = 4096
MIN_PASSWORD_LENGTH = 22  # 128 bits written in base64url
WEAK_PASSWORDS = frozenset({"", "rag", "postgres", "password", "pass", "admin", "root", "secret", "changeme", "123456",
                            "12345678", "qwerty"})
STAGED_SUFFIX = ".new"
_SAFE_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")
PRINTABLE = re.compile(r"[\x21-\x7e]+")  # visible ASCII: no space, no control character


class DbSetupError(Exception):
    """A set-up, rotation or check step failed. The text never contains a password or a verifier."""


# ---------- passwords ----------

def new_password():
    return secrets.token_urlsafe(32)  # 256 bits


def password_is_strong(password):
    return (len(password) >= MIN_PASSWORD_LENGTH and password.lower() not in WEAK_PASSWORDS
            and len(set(password)) > 4)


def scram_verifier(password, salt=None, iterations=SCRAM_ITERATIONS):
    """The SCRAM-SHA-256 verifier PostgreSQL stores (RFC 5802 / 7677): what `ALTER ROLE ... PASSWORD` accepts instead
    of a clear text password, so the password itself is never sent to the server nor written to its logs."""
    salt = os.urandom(16) if salt is None else salt
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    stored = hashlib.sha256(client_key).digest()
    b64 = [base64.b64encode(raw).decode("ascii") for raw in (salt, stored, server_key)]
    return f"SCRAM-SHA-256${iterations}:{b64[0]}${b64[1]}:{b64[2]}"


# ---------- the secret files ----------

def read_password(role, directory=None):
    """The password of `role` from its private file. A corrupted, weak or default one is refused HERE, so that no path
    (the pool, the migrator, the backup tools, set-up) ever connects with one."""
    value = secretfiles.read_secret(SECRET_FILES[role], directory)
    if not PRINTABLE.fullmatch(value):
        raise SecretError("corrupted")
    if not password_is_strong(value):
        raise SecretError("weak")
    return value


def _record_or_refuse(events, event, reason):
    """Write a security event for a change that has happened; if it cannot be written, say so loudly (non-zero), never
    carry on as if all were recorded."""
    if not events.emit(event, reason):
        raise DbSetupError("the change was made but could NOT be written to the security log")


def _log_must_work(events):
    """A security-relevant change is refused up front when its record could not be kept."""
    try:
        secretfiles.ensure_private_dir(events.directory)
    except (OSError, SecretError):
        raise DbSetupError("the security log directory is not usable: refusing to change security settings") from None
    if not events.check():
        raise DbSetupError("the security log cannot be written: refusing to change security settings")


def ensure_secrets(directory=None, events=None):
    """Create the password file of every role that has none (a strong random one); an existing file is never touched.
    Returns the roles whose file was just made."""
    missing = []
    for role in ROLES:
        try:
            secretfiles.read_secret(SECRET_FILES[role], directory)
        except SecretError as error:
            if error.code != "missing":
                raise
            missing.append(role)
    if not missing:
        return []
    events = events or SecurityLog()
    _log_must_work(events)
    for role in missing:
        secretfiles.write_secret(SECRET_FILES[role], new_password() + "\n", directory)
    _record_or_refuse(events, "secrets_created", "ok")
    return missing


def passwords(directory=None):
    """{role: password} for the three roles; refuses weak ones and the same password for two roles."""
    try:
        found = {role: read_password(role, directory) for role in ROLES}
    except SecretError as error:
        if error.code == "corrupted":
            raise DbSetupError("a password file is corrupted (it must be one line of printable characters)") from None
        if error.code == "weak":
            raise DbSetupError("a password file holds a weak or default password: delete it and run db-secrets "
                               "again") from None
        raise
    if len(set(found.values())) != len(found):
        raise DbSetupError("two roles share one password: each role needs its own")
    return found


# ---------- how the application reaches the server ----------

def conninfo(role):
    """Host, port, database and user of `role`; the password is added separately when connecting."""
    return make_conninfo(host=config.DB_HOST, port=config.DB_PORT, dbname=config.DB_NAME, user=role)


def url_with_password(role, directory=None):
    """A URL that includes the password: kept in memory for the backup tools, never printed (describe() omits it)."""
    return (f"postgresql://{role}:{quote(read_password(role, directory), safe='')}"
            f"@{config.DB_HOST}:{config.DB_PORT}/{config.DB_NAME}")


# ---------- permissions of the running application ----------

def quote_ident(name):
    return '"' + name.replace('"', '""') + '"'


def apply_runtime_grants(conn, schema):
    """Make rag_app's rights exactly RUNTIME_GRANTS in `schema`. Run by rag_migrator (the owner), after migrations."""
    s = quote_ident(schema)
    existing = {row["tablename"] for row in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = %s",
                                                         (schema,))}
    conn.execute(f"GRANT USAGE ON SCHEMA {s} TO {APP}")
    conn.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA {s} FROM {APP}")
    conn.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {s} FROM {APP}")
    for table, privileges in RUNTIME_GRANTS.items():
        if table in existing:
            conn.execute(f"GRANT {', '.join(privileges)} ON {s}.{quote_ident(table)} TO {APP}")
    conn.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {s} TO {APP}")


def grants_in_place(conn, schema):
    """True when rag_app already has exactly the rights of RUNTIME_GRANTS (a read-only look, so rag_app may ask)."""
    existing = {row["tablename"] for row in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = %s",
                                                         (schema,))}
    if not existing <= set(RUNTIME_GRANTS):
        return False  # a table nobody decided about yet
    if not conn.execute("SELECT has_schema_privilege(%s, %s, 'USAGE') AS ok", (APP, schema)).fetchone()["ok"]:
        return False
    for table in existing:
        name = f"{quote_ident(schema)}.{quote_ident(table)}"
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            has = conn.execute("SELECT has_table_privilege(%s, %s, %s) AS ok", (APP, name, privilege)).fetchone()["ok"]
            if has != (privilege in RUNTIME_GRANTS[table]):
                return False
    return True


# ---------- talking to the server as the bootstrap superuser (inside the container) ----------

def _docker_exec(container):
    if container:
        return ["docker", "exec", "-i", container]
    return ["docker", "compose", "-f", str(Path(config.ROOT) / "docker-compose.yml"), "exec", "-T", "postgres"]


def _scrub(text):
    """The first line of psql's complaint, without any statement text (it could carry a verifier)."""
    lines = [line for line in text.splitlines() if line.strip() and "SCRAM-SHA-256$" not in line]
    return (lines[0] if lines else "psql failed")[:200]


def psql(sql, container=None, user=BOOTSTRAP, database=None):
    """Run `sql` as `user` inside the database container, the SQL on standard input: no password and no statement ever
    appears in a command line or in `docker inspect`. Returns what psql printed."""
    command = [*_docker_exec(container), "psql", "-X", "-q", "-tA", "-v", "ON_ERROR_STOP=1", "-U", user,
               "-d", database or config.DB_NAME]
    done = subprocess.run(command, input=sql.encode("utf-8"), capture_output=True)
    if done.returncode != 0:
        raise DbSetupError(f"psql as {user} failed: {_scrub(done.stderr.decode('utf-8', 'replace'))}")
    return done.stdout.decode("utf-8", "replace")


def superuser(container=None):
    """The superuser the database can be reached as from inside the container: rag_bootstrap, or (a database made by
    the old compose file) the legacy `rag`."""
    for user in (BOOTSTRAP, LEGACY):
        try:
            psql("SELECT 1", container, user)
            return user
        except DbSetupError:
            continue
    raise DbSetupError("no superuser could be reached inside the database container: is it running?")


def _role_block(role, verifier):
    return (f"ALTER ROLE {role} WITH LOGIN {NO_ATTRIBUTES} PASSWORD '{verifier}';\n"
            f"DO $$ DECLARE m regrole; BEGIN FOR m IN SELECT roleid::regrole FROM pg_auth_members "
            f"WHERE member = '{role}'::regrole LOOP EXECUTE format('REVOKE %s FROM {role}', m); END LOOP; END $$;\n")


def setup_sql(found, schema, legacy_present):
    """The whole role set-up as one idempotent script (it can be run again, and again after a failure). Contains
    verifiers, never passwords."""
    verifier = {role: scram_verifier(password) for role, password in found.items()}
    s, name = quote_ident(schema), quote_ident(config.DB_NAME)
    sql = ["DO $$ BEGIN",
           f"IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{BOOTSTRAP}') THEN CREATE ROLE {BOOTSTRAP} LOGIN "
           "SUPERUSER; END IF;",
           f"IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{MIGRATOR}') THEN CREATE ROLE {MIGRATOR} LOGIN "
           f"{NO_ATTRIBUTES}; END IF;",
           f"IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{APP}') THEN CREATE ROLE {APP} LOGIN "
           f"{NO_ATTRIBUTES}; END IF;",
           "END $$;",
           f"ALTER ROLE {BOOTSTRAP} WITH LOGIN SUPERUSER PASSWORD '{verifier[BOOTSTRAP]}';",
           _role_block(MIGRATOR, verifier[MIGRATOR]), _role_block(APP, verifier[APP]),
           # the migrator owns the database and the schema: it alone may create and change objects there
           f"ALTER DATABASE {name} OWNER TO {MIGRATOR};",
           f"CREATE SCHEMA IF NOT EXISTS {s} AUTHORIZATION {MIGRATOR};",
           f"ALTER SCHEMA {s} OWNER TO {MIGRATOR};",
           f"REVOKE ALL ON DATABASE {name} FROM PUBLIC;",
           f"REVOKE ALL ON DATABASE {name} FROM {APP};",
           f"GRANT CONNECT ON DATABASE {name} TO {APP}, {MIGRATOR};",
           f"REVOKE ALL ON SCHEMA {s} FROM PUBLIC;",
           f"GRANT USAGE ON SCHEMA {s} TO {APP};",
           # tables, sequences and views the old superuser made become the migrator's (data untouched)
           "DO $$ DECLARE r record; BEGIN FOR r IN SELECT c.relname, c.relkind FROM pg_class c JOIN pg_namespace n "
           f"ON n.oid = c.relnamespace WHERE n.nspname = '{schema}' AND c.relkind IN ('r','p','S','v','m') "
           f"AND c.relowner <> '{MIGRATOR}'::regrole AND pg_get_userbyid(c.relowner) IN ('{LEGACY}', '{BOOTSTRAP}') "
           "LOOP EXECUTE format('ALTER %s %I.%I OWNER TO " + MIGRATOR + "', "
           "CASE r.relkind WHEN 'S' THEN 'SEQUENCE' WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW' "
           f"ELSE 'TABLE' END, '{schema}', r.relname); END LOOP; END $$;"]
    if legacy_present:  # the old shared superuser can no longer log in and its password is gone
        sql.append(f"ALTER ROLE {LEGACY} NOLOGIN PASSWORD NULL;")
    # all or nothing: a failure part-way leaves the server as it was
    return "BEGIN;\n" + "\n".join(sql) + "\nCOMMIT;\n"


def role_exists(role, container, user):
    return psql(f"SELECT 1 FROM pg_roles WHERE rolname = '{role}'", container, user).strip() == "1"


LEGACY_WARNING = (
    "this database still has the old shared superuser 'rag' (password 'rag'). Setting up the three roles will: "
    "(1) make 'rag' unable to log in, anywhere, with any password; (2) change the owner of the tables to rag_migrator. "
    "No row is changed or deleted. Anything that still connects as rag:rag (scripts, test runs, other tools) will stop "
    "working. Take a backup first (python -m rag backup) and run set-up again with --adopt-legacy.")


def setup(container=None, directory=None, *, adopt_legacy=False, migrate=True, events=None):
    """Create/repair the three roles and the database permissions, then (unless `migrate` is False) bring the schema up
    to date as rag_migrator.

    Safe to repeat, and all-or-nothing on the server (one transaction). Existing data is never touched: tables keep
    their rows, only their owner changes. A database that still has the old open superuser `rag` is refused unless
    `adopt_legacy` says the operator knows what follows (see LEGACY_WARNING). Returns [(step, text)].
    """
    from rag import db  # not at import time: db imports this module

    schema = config.DB_SCHEMA or "public"
    if not (_SAFE_IDENT.fullmatch(config.DB_NAME) and _SAFE_IDENT.fullmatch(schema)):
        raise DbSetupError("the database and schema names must be plain identifiers")
    events = events or SecurityLog()
    secretfiles.ensure_private_dir(directory or config.SECRETS_DIR)
    found = passwords(directory)
    user = superuser(container)
    legacy_open = psql(f"SELECT 1 FROM pg_roles WHERE rolname = '{LEGACY}' AND rolsuper AND rolcanlogin",
                       container, user).strip() == "1"
    if legacy_open and not adopt_legacy:
        raise DbSetupError(LEGACY_WARNING)
    legacy_present = legacy_open or psql(f"SELECT 1 FROM pg_roles WHERE rolname = '{LEGACY}' AND rolsuper",
                                         container, user).strip() == "1"
    _log_must_work(events)
    try:
        psql(setup_sql(found, schema, legacy_present), container, user)
    except DbSetupError:
        events.emit("db_setup", "failed")
        raise
    steps = [("roles", f"{BOOTSTRAP}, {MIGRATOR}, {APP} are set up (passwords from the secret files)")]
    if legacy_present:
        steps.append(("legacy", f"the old shared role '{LEGACY}' can no longer log in; its tables now belong to "
                                f"{MIGRATOR} (no data was changed)"))
        _record_or_refuse(events, "legacy_role_closed", "ok")
    _record_or_refuse(events, "db_setup", "ok")
    if migrate:
        db.close()  # a pool that was made before the roles existed must not be reused
        db.migrate()
        steps.append(("migrations", "the schema is up to date; the runtime permissions are in place"))
    return steps


def rotate(role, container=None, directory=None, events=None):
    """Give `role` a new random password. The new one is kept in a staged file until the server has accepted it, so a
    failure at any point leaves the old file and the old password working together. Refused when the security log
    cannot record it; the change itself is recorded (the role only, never a value)."""
    if role not in ROLES:
        raise DbSetupError(f"unknown role {role!r}")
    events, directory = events or SecurityLog(), directory or config.SECRETS_DIR
    _log_must_work(events)
    name, short = SECRET_FILES[role], role.removeprefix("rag_")
    new = new_password()
    secretfiles.write_secret(name + STAGED_SUFFIX, new + "\n", directory)
    try:
        psql(f"ALTER ROLE {role} PASSWORD '{scram_verifier(new)}';", container, superuser(container))
    except DbSetupError:
        (Path(directory) / (name + STAGED_SUFFIX)).unlink(missing_ok=True)
        events.emit("credential_rotation_failed", short)
        raise
    os.replace(Path(directory) / (name + STAGED_SUFFIX), Path(directory) / name)
    secretfiles.verify_private(Path(directory) / name)
    _record_or_refuse(events, "credential_rotated", short)


# ---------- the check an operator runs after set-up ----------

def _login_works(role, password):
    try:
        with psycopg.connect(conninfo(role), password=password, connect_timeout=5):
            return True
    except psycopg.Error:
        return False


def _role_limited(role, password):
    """The role has none of the powers of the bootstrap role (the question the connector asks of rag_app)."""
    from rag.auth import checks
    try:
        with psycopg.connect(conninfo(role), password=password, connect_timeout=5, row_factory=dict_row) as conn:
            row = conn.execute(checks.ROLE_SQL).fetchone()
        return row is not None and not any(row.values())
    except psycopg.Error:
        return False


def _only_bootstrap_superuser(app_password):
    sql = "SELECT count(*) AS n FROM pg_roles WHERE rolsuper AND rolcanlogin AND rolname <> %s"
    try:
        with psycopg.connect(conninfo(APP), password=app_password, connect_timeout=5, row_factory=dict_row) as conn:
            return conn.execute(sql, (BOOTSTRAP,)).fetchone()["n"] == 0
    except psycopg.Error:
        return False


def verify(directory=None):
    """[(name, ok)]: what must be true after set-up. Every login attempt here is one the operator started on purpose."""
    from rag import db  # not at import time: db imports this module
    from rag.auth import checks

    found = passwords(directory)
    app_logs_in = _login_works(APP, found[APP])
    out = [("app_can_log_in", app_logs_in), ("migrator_can_log_in", _login_works(MIGRATOR, found[MIGRATOR])),
           ("bootstrap_closed_over_network", not _login_works(BOOTSTRAP, found[BOOTSTRAP])),
           ("legacy_rag_closed", not _login_works(LEGACY, LEGACY)),
           ("migrator_role_limited", _role_limited(MIGRATOR, found[MIGRATOR])),
           ("only_bootstrap_is_a_login_superuser", app_logs_in and _only_bootstrap_superuser(found[APP]))]
    for name, check in (("app_role_unprivileged", checks.db_role_unprivileged),
                        ("app_has_no_ddl", checks.db_runtime_permissions)):
        try:  # without a working login there is nothing to ask (and no pool to open)
            out.append((name, app_logs_in and bool(check())))
        except Exception:  # noqa: BLE001 - the reason is not for the screen
            out.append((name, False))
    db.close()
    return out
