"""The credential vault: how the password of an external database is kept (roadmap 6a.6 SR-7, SR-13; ADR-P16 P16.3,
P16.8).

- AES-256-GCM (authenticated encryption): a changed ciphertext, nonce, key or associated data is detected, never
  decrypted to something else.
- The associated data is `connection id + key id` and NOTHING that can change: not the owner (a connection may change
  owner later and its ciphertext must still open), not the name, the host or the time. The connection id makes a
  ciphertext copied to another connection useless; the key id makes a ciphertext sealed by one key refuse another.
- An envelope is {ciphertext (with tag), nonce, key_id, version}. `version` is the format of the envelope; `key_id`
  says which key sealed it, so keys can be replaced one by one (key rotation) without a flag day.
- The key ring is text: one key per line as `<key_id>:<32 random bytes, base64url>`. The FIRST line is the active key
  (used to seal); every line can open what it sealed. The text comes from a loader the application gives the vault
  (the private secret directory, rag/auth/secretfiles); this module knows no path, imports nothing of the application
  and keeps no key in memory between calls, so a key removed from the file is gone at once.
- Fail closed: a missing key, an unknown key id, a malformed ring or envelope, a wrong key and a tampered ciphertext
  are all VaultError with a fixed `code`. The error carries no key, no ciphertext, no plaintext and no path.
"""

import base64
import binascii
import os
import re
import secrets
import uuid
from typing import NamedTuple

KEY_BYTES = 32
NONCE_BYTES = 12
TAG_BYTES = 16
ENVELOPE_VERSION = 1
MAX_SECRET_CHARS = 1024  # a password is never longer; keeps the vault from being used to store anything else
KEY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
_AAD_PREFIX = b"rag-connector-credential\x00v1\x00"


class VaultError(Exception):
    """`code`: key_missing, key_unavailable, key_invalid, unknown_key, bad_envelope, decrypt_failed, crypto_unavailable,
    too_long or empty. Never a value."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class Sealed(NamedTuple):
    ciphertext: bytes
    nonce: bytes
    key_id: str
    version: int


def new_key_line(key_id=None):
    """A fresh key ring line: `<key_id>:<key>`. Written by the operator tools, never by the request path."""
    key_id = key_id or "k" + secrets.token_hex(4)
    if not KEY_ID.fullmatch(key_id):
        raise VaultError("key_invalid")
    return f"{key_id}:{base64.urlsafe_b64encode(os.urandom(KEY_BYTES)).decode('ascii')}"


def parse_keyring(text):
    """(active key id, {key id: key bytes}) from the key ring text. VaultError("key_invalid") for anything odd: no line,
    a duplicate id, a key that is not exactly 32 bytes of base64url."""
    keys, active = {}, None
    for raw in str(text).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key_id, separator, encoded = line.partition(":")
        if not separator or not KEY_ID.fullmatch(key_id) or key_id in keys:
            raise VaultError("key_invalid")
        try:
            key = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except (binascii.Error, ValueError):
            raise VaultError("key_invalid") from None
        if len(key) != KEY_BYTES:
            raise VaultError("key_invalid")
        keys[key_id] = key
        active = active or key_id
    if not keys:
        raise VaultError("key_invalid")
    return active, keys


def _aad(connection_id, key_id):
    return _AAD_PREFIX + str(uuid.UUID(str(connection_id))).encode("ascii") + b"\x00" + key_id.encode("ascii")


def _aesgcm(key):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:  # the optional requirements-connector.txt is not installed
        raise VaultError("crypto_unavailable") from None
    return AESGCM(key)


class Vault:
    def __init__(self, load_keyring):
        """`load_keyring()` returns the key ring text. It may raise: any exception means the key cannot be had."""
        self._load = load_keyring

    def _keyring(self):
        try:
            text = self._load()
        except Exception as error:  # noqa: BLE001 - whatever the loader says, the vault says one fixed word
            raise VaultError("key_missing" if getattr(error, "code", "") == "missing" else "key_unavailable") from None
        return parse_keyring(text)

    def active_key_id(self):
        return self._keyring()[0]

    def key_ids(self):
        """The ids of every key in the ring (raises VaultError when the ring cannot be had)."""
        return frozenset(self._keyring()[1])

    def has_key(self, key_id):
        """True when the key `key_id` can be had right now (no decryption is tried). False for any failure."""
        try:
            return key_id in self._keyring()[1]
        except VaultError:
            return False

    def seal(self, secret, connection_id):
        """Encrypt `secret` for `connection_id` with the active key."""
        if not isinstance(secret, str) or not secret:
            raise VaultError("empty")
        if len(secret) > MAX_SECRET_CHARS:
            raise VaultError("too_long")
        key_id, keys = self._keyring()
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = _aesgcm(keys[key_id]).encrypt(nonce, secret.encode("utf-8"), _aad(connection_id, key_id))
        return Sealed(ciphertext, nonce, key_id, ENVELOPE_VERSION)

    def open(self, sealed, connection_id):
        """The plaintext of `sealed` for `connection_id`; VaultError if anything at all is wrong with it."""
        ciphertext, nonce = bytes(sealed.ciphertext), bytes(sealed.nonce)
        if (
            sealed.version != ENVELOPE_VERSION
            or len(nonce) != NONCE_BYTES
            or len(ciphertext) <= TAG_BYTES
            or not KEY_ID.fullmatch(str(sealed.key_id))
        ):
            raise VaultError("bad_envelope")
        keys = self._keyring()[1]
        if sealed.key_id not in keys:
            raise VaultError("unknown_key")
        try:
            return (
                _aesgcm(keys[sealed.key_id])
                .decrypt(nonce, ciphertext, _aad(connection_id, sealed.key_id))
                .decode("utf-8")
            )
        except VaultError:
            raise
        except Exception:  # noqa: BLE001 - InvalidTag (wrong key, tampering, other connection) and nothing more is said
            raise VaultError("decrypt_failed") from None
