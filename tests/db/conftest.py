import os
import uuid

import psycopg
import pytest

from rag import config, db

pytestmark = pytest.mark.db


@pytest.fixture(scope="session")
def database():
    """A throw-away schema in the test database, migrated once and dropped at the end."""
    url = os.environ["RAG_TEST_DATABASE_URL"]
    schema = f"rag_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    mp = pytest.MonkeyPatch()
    mp.setattr(config, "DATABASE_URL", url)
    mp.setattr(config, "DB_SCHEMA", schema)
    db.pool.cache_clear()
    db.migrate()
    yield schema
    db.close()
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA {schema} CASCADE")
    mp.undo()


@pytest.fixture(autouse=True)
def clean(database):
    with db.pool().connection() as conn:
        conn.execute("TRUNCATE documents")
