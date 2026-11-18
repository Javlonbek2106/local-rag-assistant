"""Roadmap M4.1a: the principals table (migration 0005) against a real PostgreSQL.

Exactly one local owner, however many processes start together; a stable id; and the building blocks of the later
"claim, not remap": the claim-consistency constraint and the compare-and-set that lets exactly one claimant win.
"""
import threading
import uuid

import psycopg
import pytest

from rag import db
from rag.auth.principals import PrincipalError, Principals

pytestmark = pytest.mark.db


@pytest.fixture
def restore(database):
    """Puts the principals table back exactly as it was, whatever the test did to it."""
    with db.pool().connection() as conn:
        saved = conn.execute("SELECT * FROM principals").fetchall()
    yield
    with db.pool().connection() as conn:
        conn.execute("DELETE FROM principals")
        for row in saved:
            conn.execute("INSERT INTO principals (id, kind, display_label, user_id, claimed_at, disabled_at, "
                         "created_at) VALUES (%(id)s, %(kind)s, %(display_label)s, %(user_id)s, %(claimed_at)s, "
                         "%(disabled_at)s, %(created_at)s)", row)


def count(conn, where="true"):
    return conn.execute(f"SELECT count(*) AS n FROM principals WHERE {where}").fetchone()["n"]


def test_there_is_exactly_one_local_owner_after_the_migration(database):
    with db.pool().connection() as conn:
        assert count(conn, "kind = 'local-owner'") == 1 and count(conn) == 1
    owner = Principals().local_owner()
    assert owner["kind"] == "local-owner" and str(uuid.UUID(owner["id"])) == owner["id"]


def test_the_id_is_stable_across_restarts_and_repeated_migrations(database):
    first = Principals().local_owner()["id"]
    db.migrate()
    db.close()  # a new pool, as after a restart
    assert Principals().local_owner()["id"] == first


def test_a_second_local_owner_cannot_be_made(database):
    with db.pool().connection() as conn, pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute("INSERT INTO principals (kind) VALUES ('local-owner')")


def test_many_starts_at_once_still_leave_exactly_one(database):
    """What the migration's INSERT does, from several connections at the same moment: one row, no error."""
    barrier, errors = threading.Barrier(4), []

    def start():
        try:
            with db.pool().connection() as conn:
                barrier.wait(10)
                conn.execute("INSERT INTO principals (kind) VALUES ('local-owner') ON CONFLICT DO NOTHING")
        except Exception as error:  # noqa: BLE001 - every failure must reach the assertion
            errors.append(error)

    threads = [threading.Thread(target=start) for _ in range(4)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    with db.pool().connection() as conn:
        assert errors == [] and count(conn, "kind = 'local-owner'") == 1


def test_a_principal_is_either_claimed_completely_or_not_at_all(database, restore):
    with db.pool().connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE principals SET user_id = gen_random_uuid() WHERE kind = 'local-owner'")
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE principals SET claimed_at = now() WHERE kind = 'local-owner'")
        conn.execute("UPDATE principals SET user_id = gen_random_uuid(), claimed_at = now() WHERE kind = 'local-owner'")


def test_of_two_claims_made_at_the_same_moment_exactly_one_wins(database, restore):
    """The compare-and-set of the later phase: `WHERE user_id IS NULL` lets one UPDATE change a row, the others none."""
    barrier, changed, errors = threading.Barrier(4), [], []

    def claim():
        try:
            with db.pool().connection() as conn:
                barrier.wait(10)
                result = conn.execute("UPDATE principals SET user_id = gen_random_uuid(), claimed_at = now() "
                                      "WHERE kind = 'local-owner' AND user_id IS NULL")
                changed.append(result.rowcount)
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=claim) for _ in range(4)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert errors == [] and sorted(changed) == [0, 0, 0, 1]
    with db.pool().connection() as conn:
        assert count(conn, "user_id IS NOT NULL AND claimed_at IS NOT NULL") == 1


def test_a_claim_never_rewrites_the_principal_it_only_fills_the_claim_columns(database, restore):
    with db.pool().connection() as conn:
        before = conn.execute("SELECT id, kind, created_at FROM principals").fetchone()
        conn.execute("UPDATE principals SET user_id = gen_random_uuid(), claimed_at = now() "
                     "WHERE kind = 'local-owner' AND user_id IS NULL")
        after = conn.execute("SELECT id, kind, created_at FROM principals").fetchone()
    assert before == after
    assert Principals().local_owner()["id"] == str(before["id"])  # the same principal, now claimed


def test_without_a_local_owner_the_check_fails_and_nothing_is_guessed(database, restore):
    with db.pool().connection() as conn:
        conn.execute("DELETE FROM principals")
    with pytest.raises(PrincipalError) as error:
        Principals().local_owner()
    assert error.value.code == "not_exactly_one"


def test_a_disabled_local_owner_is_not_usable(database, restore):
    with db.pool().connection() as conn:
        conn.execute("UPDATE principals SET disabled_at = now() WHERE kind = 'local-owner'")
    with pytest.raises(PrincipalError) as error:
        Principals().local_owner()
    assert error.value.code == "disabled"


def test_an_unreachable_database_is_a_failure_that_carries_no_connection_text(database, monkeypatch):
    class Broken:
        def connection(self):
            raise ConnectionError("postgresql://rag:secret-password@db/rag refused")

    monkeypatch.setattr(db, "pool", lambda: Broken())
    with pytest.raises(PrincipalError) as error:
        Principals().local_owner()
    assert error.value.code == "unreachable" and "secret-password" not in str(error.value)
