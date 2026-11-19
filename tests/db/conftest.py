import psycopg
import pytest

from rag import config

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def clean(database):
    # As the administrator of the test database, not through the application's pool: with RAG_TEST_HARDENED=1 the
    # pool is rag_app, which (rightly) may not TRUNCATE.
    options = f"-c search_path={config.DB_SCHEMA}"
    with psycopg.connect(config.DATABASE_URL, options=options, autocommit=True) as conn:
        conn.execute("TRUNCATE documents, document_versions")  # both in one statement: they reference each other
