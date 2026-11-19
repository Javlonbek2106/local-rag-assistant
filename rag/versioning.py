"""Version checks that need both PostgreSQL and Qdrant: verifying an index, rolling back, registering old documents.

PostgreSQL decides which version is active (rag.db); Qdrant only holds the vectors. A version may become (or be made)
active only after its vectors in Qdrant have been counted and found complete.
"""
import hashlib
import logging

from rag import db, storage, store

log = logging.getLogger(__name__)


class IndexIncomplete(Exception):
    """The version's vectors in Qdrant are not exactly the chunks that were meant to be indexed."""


class RollbackError(Exception):
    """A rollback was refused; nothing changed. `status` is the HTTP status the API answers with."""

    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def verify(client, document_id, version_id, file_hash, expected):
    """Raise IndexIncomplete unless Qdrant holds exactly `expected` chunks of this version, all of them belonging to
    this document and this file (and, when the hash is known, saying so themselves). A partial or leftover index
    fails the check; nothing is activated on a guess."""
    found = store.count_version(client, version_id)
    if found != expected:
        raise IndexIncomplete(f"Qdrant da {found} ta bo'lak bor, {expected} ta kerak")
    matching = store.count_version(client, version_id, document_id=document_id, file_hash=file_hash,
                                   chunks_total=expected)
    if matching != expected:
        raise IndexIncomplete(f"{expected - matching} ta bo'lak boshqa hujjat yoki fayl ma'lumotini tutadi")


def rollback(repo, client, name, version_number):
    """Make an earlier version (one that was active before) the active one again, without re-embedding anything.

    Refuses, changing nothing, when the version does not exist, is the active one, was never active (queued, failed,
    still being indexed), or its vectors are missing or incomplete. Returns the version that is now active.

    A successful rollback is a durable decision (see db.switch_active): versions that were queued or being indexed
    before it will not replace the chosen version when they finish. A refused rollback changes nothing at all, so
    those versions stay free to activate.
    """
    version = repo.get_version(name, version_number)
    if version is None:
        raise RollbackError("Versiya topilmadi", 404)
    if version["active"]:
        raise RollbackError("Bu versiya allaqachon faol")
    if version["status"] != "SUPERSEDED" or version["chunk_count"] is None:
        raise RollbackError(f"Bu versiyaga qaytib bo'lmaydi (holati: {version['status']})")
    try:
        verify(client, version["document_id"], version["id"], version["file_hash"], version["chunk_count"])
    except IndexIncomplete as error:
        raise RollbackError(f"Versiya indeksi to'liq emas, qaytib bo'lmaydi: {error}") from error
    try:
        generation = repo.switch_active(version["id"])  # checks the status again under the lock
    except db.VersionConflict as error:
        raise RollbackError(str(error)) from error
    log.info("%s rolled back to version %d (activation generation %s)", name, version_number, generation)
    return repo.get_version(name, version_number)


def backfill_legacy(repo, client):
    """Register documents indexed before versioning (the migration made each of them version 1): give their chunks in
    Qdrant the version id and fill in the file hash. Safe to repeat and to run from several processes at once."""
    done = 0
    for version in repo.versions_without_hash():
        store.tag_legacy(client, version["document_id"], version["id"])
        try:
            path = storage.resolve(version["file_path"])
            if path.is_file():
                with path.open("rb") as f:
                    repo.set_file_hash(version["id"], hashlib.file_digest(f, "sha256").hexdigest())
        except (OSError, ValueError):
            log.warning("could not hash the stored file of version %s", version["id"])
        done += 1
    if done:
        log.info("registered %d document(s) indexed before versioning", done)
    return done
