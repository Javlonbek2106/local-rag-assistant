"""The external database connector (roadmap 6a, milestone M4.1b): connections, their encrypted credentials, connection
tests, and the schema of external PostgreSQL and SQL Server databases with the descriptions people write for it.

What this package is NOT (yet): it does not generate or run SQL, does not copy data rows, does not ingest anything.
External databases are only ever asked for catalog metadata and `SELECT 1`, on read-only connections.

Boundary rules (tests/unit/test_connector_boundaries.py checks them):
- no import of the document side of the application (ingest, store, versioning, worker, storage, answer) and no import
  of rag.auth: the API layer (rag/api.py) hands the principal in as a plain id;
- `drivers` import neither rag.db nor rag.auth; only `repository` touches the internal database, and every statement
  of it filters by the owner;
- third-party libraries for the connector (cryptography, python-tds) are imported lazily, inside the functions that
  need them, so the light API and the CI that installs only the core requirements keep working
  (requirements-connector.txt).
"""
