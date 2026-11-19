"""Operator commands for the credential vault (docs/connector.md).

    python -m rag connector key-init     make the encryption key if there is none (never replaces one)
    python -m rag connector key-rotate   add a NEW active key; old keys stay in the ring so nothing becomes unreadable
    python -m rag connector rewrap       re-encrypt every stored credential under the active key (safe to repeat)
    python -m rag connector verify       open every stored credential with the ring (read-only); counts only

The key ring is one file in the private secret directory (config.ENCRYPTION_KEY_FILE): one `<key id>:<key>` per line,
the first line is the active key. It is outside PostgreSQL, the backups, the repository and the image on purpose: a
database backup alone cannot open a credential. Lose the ring and the stored credentials cannot be recovered (the
connections then say `needs_credentials` and are re-entered); keep a copy of the ring apart from the database backups.

Nothing here prints a key, a password or a ciphertext. A key change is written to the security log first: if it cannot
be written, the change is refused.
"""

import argparse
import sys

from rag import config
from rag.auth import secretfiles
from rag.auth.events import SecurityLog
from rag.connector import repository
from rag.connector.vault import Vault, VaultError, new_key_line, parse_keyring
from rag.connector_wiring import read_key_ring


def _log_ready(events):
    try:
        secretfiles.ensure_private_dir(events.directory)
    except (OSError, secretfiles.SecretError):
        print("error: the security log directory is not usable: refusing to change the key", file=sys.stderr)
        return False
    if not events.check():
        print("error: the security log cannot be written: refusing to change the key", file=sys.stderr)
        return False
    return True


def _write_ring(lines):
    secretfiles.write_secret(config.ENCRYPTION_KEY_FILE, "\n".join(lines) + "\n")


def _recorded(events):
    if not events.emit("key_created", "ok"):
        print("error: the key was made but could NOT be written to the security log", file=sys.stderr)
        return 1
    return 0


def key_init(events):
    try:
        parse_keyring(read_key_ring())
        print("the encryption key already exists; nothing changed")
        return 0
    except secretfiles.SecretError as error:
        if error.code != "missing":
            print(f"error: the key file is not usable ({error.code}); it is NOT replaced", file=sys.stderr)
            return 1
    except VaultError:
        print("error: the key file is damaged; it is NOT replaced", file=sys.stderr)
        return 1
    if not _log_ready(events):
        return 1
    _write_ring([new_key_line()])
    if _recorded(events):
        return 1
    print("the encryption key was created")
    return 0


def key_rotate(events):
    try:
        text = read_key_ring()
        parse_keyring(text)
    except (secretfiles.SecretError, VaultError):
        print("error: there is no usable key ring; run key-init first", file=sys.stderr)
        return 1
    if not _log_ready(events):
        return 1
    _write_ring([new_key_line(), *[line.strip() for line in text.splitlines() if line.strip()]])
    if _recorded(events):
        return 1
    print("a new active key was added; run `rewrap` to move the stored credentials to it")
    return 0


def _walk():
    for owner in repository.principals():
        for connection_id, sealed in repository.all_sealed(owner):
            yield owner, connection_id, sealed


def verify(vault):
    ok, failed = 0, {}
    for _owner, connection_id, sealed in _walk():
        try:
            vault.open(sealed, connection_id)
            ok += 1
        except VaultError as error:
            failed[error.code] = failed.get(error.code, 0) + 1
    detail = f" ({', '.join(f'{k}: {v}' for k, v in sorted(failed.items()))})" if failed else ""
    print(f"credentials that open: {ok}; that do not: {sum(failed.values())}{detail}")
    return 0 if not failed else 1


def rewrap(vault, events):
    try:
        active = vault.active_key_id()
    except VaultError as error:
        print(f"error: no usable key ({error.code})", file=sys.stderr)
        return 1
    if not _log_ready(events):
        return 1
    moved = kept = 0
    for owner, connection_id, sealed in _walk():
        if sealed.key_id == active:
            kept += 1
            continue
        try:
            new = vault.seal(vault.open(sealed, connection_id), connection_id)
        except VaultError as error:
            print(f"error: a credential could not be opened ({error.code}); nothing more was changed", file=sys.stderr)
            return 1
        repository.replace_sealed(owner, connection_id, new)
        moved += 1
    if moved and not events.emit("key_rewrapped", "ok"):
        print("error: the change was made but could NOT be written to the security log", file=sys.stderr)
        return 1
    print(f"re-encrypted: {moved}; already under the active key: {kept}")
    return 0


def main(argv):
    parser = argparse.ArgumentParser(prog="python -m rag connector", description="Credential vault tools.")
    parser.add_argument("command", choices=("key-init", "key-rotate", "rewrap", "verify"))
    command = parser.parse_args(argv).command
    events, vault = SecurityLog(), Vault(read_key_ring)
    try:
        if command == "key-init":
            return key_init(events)
        if command == "key-rotate":
            return key_rotate(events)
        return rewrap(vault, events) if command == "rewrap" else verify(vault)
    except secretfiles.SecretError as error:
        print(f"error: {error.code}", file=sys.stderr)
        return 1
