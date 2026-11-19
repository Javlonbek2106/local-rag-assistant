"""Operator commands for the hardened PostgreSQL set-up (docs/postgres-hardening.md).

    python -m rag db-secrets                  create the password files that are missing; prints the directory
    python -m rag db-setup  [--container N] [--adopt-legacy] [--roles-only]
                                              create/repair the roles, adopt an old database, migrate, check
    python -m rag db-rotate --role ROLE       new random password for app, migrator or bootstrap
    python -m rag db-check                    the checks that must hold after set-up (also the last step of db-setup)

Nothing here prints a password, a verifier or a connection string with a password. Every change of a credential or of
the security set-up is written to the security log first-class: when it cannot be written, the change is refused.
Exit codes: 0 done, 1 failed, 3 an old database needs the operator's explicit --adopt-legacy.
"""
import argparse
import sys

from rag import config, db, dbroles
from rag.auth.secretfiles import SecretError

ROLE_NAMES = {"app": dbroles.APP, "migrator": dbroles.MIGRATOR, "bootstrap": dbroles.BOOTSTRAP}


def _parser(prog, description):
    return argparse.ArgumentParser(prog=f"python -m rag {prog}", description=description)


def _fail(error):
    print(f"error: {error.code if isinstance(error, SecretError) else error}", file=sys.stderr)
    return 1


def secrets_main(argv):
    _parser("db-secrets", "Create the missing password files (strong, random, private).").parse_args(argv)
    try:
        made = dbroles.ensure_secrets()
    except (dbroles.DbSetupError, SecretError) as error:
        return _fail(error)
    for role in made:
        print(f"created the password file of {role}", file=sys.stderr)
    print(config.SECRETS_DIR)  # the one line on standard output: the directory, for start.bat and docker compose
    return 0


def _report(results):
    for name, ok in results:
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}")
    return 0 if all(ok for _, ok in results) else 1


def check_main(argv):
    _parser("db-check", "Check the roles and permissions of the running set-up.").parse_args(argv)
    config.DB_HARDENED = True
    try:
        return _report(dbroles.verify())
    except (dbroles.DbSetupError, SecretError) as error:
        return _fail(error)


def setup_main(argv):
    parser = _parser("db-setup", "Create or repair the three roles, adopt an existing database, migrate, then check.")
    parser.add_argument("--container", help="Docker container that runs PostgreSQL (default: the compose service)")
    parser.add_argument("--adopt-legacy", action="store_true",
                        help="an old database with the shared superuser 'rag' is taken over: that login is closed for "
                             "good (take a backup first)")
    parser.add_argument("--roles-only", action="store_true",
                        help="make the roles and permissions but do not migrate (for an empty database that a backup "
                             "is about to be restored into)")
    args = parser.parse_args(argv)
    config.DB_HARDENED = True  # this command only makes sense for the hardened set-up
    try:
        for _step, text in dbroles.setup(args.container, adopt_legacy=args.adopt_legacy, migrate=not args.roles_only):
            print(f"  done  {text}")
        return _report(dbroles.verify()) if not args.roles_only else 0
    except dbroles.DbSetupError as error:
        if str(error) == dbroles.LEGACY_WARNING:
            print(f"NOT DONE: {error}", file=sys.stderr)
            return 3
        return _fail(error)
    except SecretError as error:
        return _fail(error)
    finally:
        db.close()


def rotate_main(argv):
    parser = _parser("db-rotate", "Give a role a new random password (the old one stops working).")
    parser.add_argument("--role", required=True, choices=sorted(ROLE_NAMES))
    parser.add_argument("--container", help="Docker container that runs PostgreSQL (default: the compose service)")
    args = parser.parse_args(argv)
    try:
        dbroles.rotate(ROLE_NAMES[args.role], args.container)
    except (dbroles.DbSetupError, SecretError) as error:
        return _fail(error)
    note = " Restart is not needed: new connections read the new password." if args.role != "bootstrap" else ""
    print(f"the password of {ROLE_NAMES[args.role]} was changed.{note}")
    return 0
