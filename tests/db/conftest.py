import pytest

from rag import db

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def clean(database):
    with db.pool().connection() as conn:
        conn.execute("TRUNCATE documents, document_versions")  # both in one statement: they reference each other
