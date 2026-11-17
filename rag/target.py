"""One place that says WHICH PostgreSQL, Qdrant collection and storage folder an operation works on.

Backup, restore and reconcile take a Target instead of reading the global configuration, so a restore drill can point
them at a throw-away environment while the normal application keeps using its own. `Target.from_config()` is the
running application's own data.
"""
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from psycopg.rows import dict_row
from qdrant_client import QdrantClient

from rag import config


@dataclass(frozen=True)
class Target:
    database_url: str
    qdrant_url: str
    collection: str
    storage_dir: Path
    db_schema: str | None = None

    @classmethod
    def from_config(cls):
        return cls(config.DATABASE_URL, config.QDRANT_URL, config.COLLECTION, config.STORAGE_DIR, config.DB_SCHEMA)

    def connect(self, **kwargs):
        """A new PostgreSQL connection (rows as dicts). The caller closes it."""
        options = {"options": f"-c search_path={self.db_schema}"} if self.db_schema else {}
        return psycopg.connect(self.database_url, row_factory=dict_row, **options, **kwargs)

    def qdrant(self, timeout=60):
        return QdrantClient(url=self.qdrant_url, timeout=timeout)

    def database_parts(self):
        """User, host, port and database name taken from the URL; never the password."""
        parts = urlsplit(self.database_url)
        return {"user": parts.username, "host": parts.hostname, "port": parts.port, "database": parts.path.lstrip("/")}

    def describe(self):
        """What may be shown in logs, reports and manifests: no credentials anywhere."""
        db = self.database_parts()
        qdrant = urlsplit(self.qdrant_url)
        return {"postgres": {"host": db["host"], "port": db["port"], "database": db["database"],
                             "schema": self.db_schema},
                "qdrant": {"host": qdrant.hostname, "port": qdrant.port, "collection": self.collection},
                "storage_dir": str(self.storage_dir)}
