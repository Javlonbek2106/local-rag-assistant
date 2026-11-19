"""Uploaded files on disk, one folder per document version:

    <STORAGE_DIR>/documents/<document id>/versions/<version id>/original<.ext>

The extension tells the parser what format the file is; the real name lives in the database. A version's file is
never overwritten by a later upload. Files stored before versioning stay where they were
(<STORAGE_DIR>/<document id>/<name>): the version row keeps the path it was saved under.
"""
import os
import re
import shutil

from rag import config

EXTENSION = re.compile(r"\.[a-z0-9]{1,10}")


def temp_dir():
    """Where uploads are first written. On the same volume as the final place, so the move is atomic."""
    return config.STORAGE_DIR / ".uploading"


def relative_path(doc_id, version_id, name):
    """Where a new version's file goes, relative to STORAGE_DIR. Built from ids and a checked extension only."""
    suffix = os.path.splitext(name)[1].lower()
    if suffix and not EXTENSION.fullmatch(suffix):
        raise ValueError(f"bad file extension: {suffix!r}")
    return f"documents/{doc_id}/versions/{version_id}/original{suffix}"


def resolve(relative):
    """The absolute path of a stored file. Refuses anything that would leave STORAGE_DIR."""
    root = config.STORAGE_DIR.resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"path outside the storage folder: {relative!r}")
    return path


def save(relative, temp_file):
    """Move a fully written temp file into place. A worker never sees a half-written file."""
    target = resolve(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(temp_file, target)
    return target


def remove(doc_id):
    """Delete every file of a document: all versions, and the pre-versioning folder if there is one."""
    shutil.rmtree(config.STORAGE_DIR / "documents" / str(doc_id), ignore_errors=True)
    shutil.rmtree(config.STORAGE_DIR / str(doc_id), ignore_errors=True)
