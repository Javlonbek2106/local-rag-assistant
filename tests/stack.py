"""A throw-away PostgreSQL + Qdrant pair in Docker, on free loopback ports, separate from the running system.

Used by the backup/restore integration tests and by scripts/restore_drill.py. Nothing here touches the compose stack
the application uses: container names, ports and (anonymous) volumes are all new, and `stop()` removes them.
"""
import json
import shutil
import socket
import subprocess
import time
import urllib.request
import uuid


def docker_available():
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Stack:
    PG_IMAGE, QDRANT_IMAGE = "postgres:17-alpine", "qdrant/qdrant:v1.20.0"

    def __init__(self, label="t", with_qdrant=True):
        self.with_qdrant = with_qdrant
        suffix = uuid.uuid4().hex[:8]
        self.pg_container, self.qdrant_container = f"ragtest-{label}-pg-{suffix}", f"ragtest-{label}-qd-{suffix}"
        self.pg_port, self.qdrant_port = free_port(), free_port()

    @property
    def database_url(self):
        return f"postgresql://rag:rag@127.0.0.1:{self.pg_port}/rag"

    def database_url_for(self, name):
        return f"postgresql://rag:rag@127.0.0.1:{self.pg_port}/{name}"

    @property
    def qdrant_url(self):
        return f"http://127.0.0.1:{self.qdrant_port}"

    def _run(self, *args):
        result = subprocess.run(["docker", *args], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"docker {args[0]} failed: {result.stderr.strip()[:300]}")
        return result.stdout.strip()

    def start(self, timeout=90):
        self._run("run", "-d", "--rm", "--name", self.pg_container, "-e", "POSTGRES_USER=rag", "-e",
                  "POSTGRES_PASSWORD=rag", "-e", "POSTGRES_DB=rag", "-p", f"127.0.0.1:{self.pg_port}:5432",
                  self.PG_IMAGE)
        if self.with_qdrant:
            self._run("run", "-d", "--rm", "--name", self.qdrant_container, "-e", "QDRANT__TELEMETRY_DISABLED=true",
                      "-p", f"127.0.0.1:{self.qdrant_port}:6333", self.QDRANT_IMAGE)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._pg_ready() and (self._qdrant_ready() or not self.with_qdrant):
                return self
            time.sleep(1)
        self.stop()
        raise RuntimeError("the test stack did not become ready in time")

    def _pg_ready(self):
        # pg_isready answers during the first start-up too, before the database is usable: ask with a real query
        probe = subprocess.run(["docker", "exec", self.pg_container, "psql", "-U", "rag", "-d", "rag", "-tAc",
                                "SELECT 1"], capture_output=True, text=True)
        return probe.returncode == 0 and probe.stdout.strip() == "1"

    def _qdrant_ready(self):
        try:
            with urllib.request.urlopen(f"{self.qdrant_url}/collections", timeout=2) as response:
                return json.load(response).get("status") == "ok"
        except Exception:  # noqa: BLE001
            return False

    def create_database(self, name):
        self._run("exec", self.pg_container, "psql", "-U", "rag", "-d", "rag", "-c", f"CREATE DATABASE {name}")
        return self.database_url_for(name)

    def stop(self):
        for name in (self.pg_container, self.qdrant_container):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
