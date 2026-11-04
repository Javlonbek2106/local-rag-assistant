"""Run the web app: python -m rag"""
import logging

import uvicorn

from rag import config
from rag.api import create_app

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    uvicorn.run(create_app(), host=config.HOST, port=config.PORT, log_level="warning")
