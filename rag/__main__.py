"""Run the app.

    python -m rag            web API + worker together (what start.bat does)
    python -m rag api        only the web API
    python -m rag worker     only the worker, restarted automatically if it exits

Operations (each prints its own --help):
    python -m rag reconcile  read-only check of PostgreSQL, Qdrant and the stored files against each other
    python -m rag backup     PostgreSQL + storage + Qdrant snapshot into one verified backup folder
    python -m rag restore    restore a backup into an EMPTY, separate environment
    python -m rag db-secrets | db-setup | db-rotate | db-check    hardened PostgreSQL roles (docs/postgres-hardening.md)
    python -m rag connector key-init | key-rotate | rewrap | verify    credential vault (docs/connector.md)
"""
import importlib
import logging
import sys

import uvicorn

from rag import config
from rag.api import create_app
from rag.worker import Supervisor


def serve():
    uvicorn.run(create_app(), host=config.HOST, port=config.PORT, log_level="warning")


OPERATIONS = {"reconcile": ("rag.reconcile", "main"), "backup": ("rag.backup", "main"),
              "restore": ("rag.backup", "restore_main"), "db-secrets": ("rag.dbadmin", "secrets_main"),
              "db-setup": ("rag.dbadmin", "setup_main"), "db-rotate": ("rag.dbadmin", "rotate_main"),
              "db-check": ("rag.dbadmin", "check_main"),
              "connector": ("rag.connectorcli", "main")}


def main(argv):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    mode = argv[1] if len(argv) > 1 else "all"
    if mode in OPERATIONS:
        module, function = OPERATIONS[mode]
        return getattr(importlib.import_module(module), function)(argv[2:])
    if mode not in ("all", "api", "worker"):
        sys.exit(__doc__)
    supervisor = Supervisor().start() if mode in ("all", "worker") else None
    try:
        if mode == "worker":
            supervisor.join()
        else:
            serve()
    except KeyboardInterrupt:
        pass
    finally:
        if supervisor:
            supervisor.stop()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
