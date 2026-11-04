"""Run the app.

    python -m rag            web API + worker together (what start.bat does)
    python -m rag api        only the web API
    python -m rag worker     only the worker, restarted automatically if it exits
"""
import logging
import sys

import uvicorn

from rag import config
from rag.api import create_app
from rag.worker import Supervisor


def serve():
    uvicorn.run(create_app(), host=config.HOST, port=config.PORT, log_level="warning")


def main(argv):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    mode = argv[1] if len(argv) > 1 else "all"
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
    main(sys.argv)
