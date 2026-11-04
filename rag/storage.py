"""Uploaded files on disk: <STORAGE_DIR>/<document id>/<original name>.

The original name keeps its extension, which tells the parser what format the file is.
"""
import os
import shutil

from rag import config


def temp_dir():
    """Where uploads are first written. On the same volume as the final place, so the move is atomic."""
    return config.STORAGE_DIR / ".uploading"


def path_for(doc_id, name):
    return config.STORAGE_DIR / str(doc_id) / name


def save(doc_id, name, temp_file):
    """Move a fully written temp file into place. A worker never sees a half-written file."""
    target = path_for(doc_id, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(temp_file, target)
    return target


def remove(doc_id):
    shutil.rmtree(config.STORAGE_DIR / str(doc_id), ignore_errors=True)
