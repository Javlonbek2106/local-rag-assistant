"""M4.1b: the credential vault. AES-256-GCM, associated data = connection id + key id, fail closed."""

import base64
import os
import uuid

import pytest

pytest.importorskip("cryptography")

from rag.connector.vault import (  # noqa: E402
    ENVELOPE_VERSION,
    KEY_BYTES,
    MAX_SECRET_CHARS,
    Sealed,
    Vault,
    VaultError,
    new_key_line,
    parse_keyring,
)

SECRET = "Sentinel-Passw0rd!-ÜñíçødЕ"
CONNECTION = str(uuid.uuid4())


def ring(*lines):
    return "\n".join(lines) + "\n"


@pytest.fixture
def key_line():
    return new_key_line("k1")


@pytest.fixture
def vault(key_line):
    return Vault(lambda: ring(key_line))


def code(callable_, *args):
    with pytest.raises(VaultError) as error:
        callable_(*args)
    return error.value.code


# ---------- the round trip and what an envelope is ----------


def test_a_secret_survives_a_round_trip_and_is_not_in_the_ciphertext(vault):
    sealed = vault.seal(SECRET, CONNECTION)
    assert vault.open(sealed, CONNECTION) == SECRET
    assert SECRET.encode() not in sealed.ciphertext and sealed.key_id == "k1" and sealed.version == ENVELOPE_VERSION
    assert len(sealed.nonce) == 12 and len(sealed.ciphertext) == len(SECRET.encode()) + 16  # plaintext + GCM tag


def test_every_sealing_uses_a_new_nonce(vault):
    first, second = vault.seal(SECRET, CONNECTION), vault.seal(SECRET, CONNECTION)
    assert first.nonce != second.nonce and first.ciphertext != second.ciphertext


def test_the_key_made_by_the_tool_is_32_random_bytes_and_every_line_is_different():
    a, b = new_key_line(), new_key_line()
    assert a != b
    key_id, _, encoded = a.partition(":")
    assert len(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))) == KEY_BYTES and key_id.startswith("k")


# ---------- associated data: connection id and key id, nothing that can change ----------


def test_a_ciphertext_copied_to_another_connection_does_not_open(vault):
    sealed = vault.seal(SECRET, CONNECTION)
    assert code(vault.open, sealed, str(uuid.uuid4())) == "decrypt_failed"


def test_the_owner_is_not_part_of_the_binding_so_a_change_of_owner_cannot_lock_a_credential_out(vault):
    """The vault is never told who the owner is: the same ciphertext opens whoever owns the connection."""
    sealed = vault.seal(SECRET, CONNECTION)
    assert vault.open(sealed, CONNECTION) == SECRET
    assert "owner" not in Vault.seal.__code__.co_varnames and "owner" not in Vault.open.__code__.co_varnames


def test_a_ciphertext_relabelled_with_another_key_id_does_not_open():
    a = new_key_line("ka")
    b = "kb:" + a.partition(":")[2]  # the SAME key bytes under another id: only the associated data tells them apart
    sealed = Vault(lambda: ring(a)).seal(SECRET, CONNECTION)
    both = Vault(lambda: ring(a, b))
    relabelled = Sealed(sealed.ciphertext, sealed.nonce, "kb", sealed.version)
    assert code(both.open, relabelled, CONNECTION) == "decrypt_failed"  # the key id is in the associated data too
    assert both.open(sealed, CONNECTION) == SECRET


# ---------- wrong key, tampering, malformed envelopes ----------


def test_a_wrong_key_with_the_same_id_does_not_open():
    sealed = Vault(lambda: ring(new_key_line("k1"))).seal(SECRET, CONNECTION)
    assert code(Vault(lambda: ring(new_key_line("k1"))).open, sealed, CONNECTION) == "decrypt_failed"


def test_an_unknown_key_id_is_reported_as_such(vault):
    sealed = vault.seal(SECRET, CONNECTION)
    assert code(Vault(lambda: ring(new_key_line("other"))).open, sealed, CONNECTION) == "unknown_key"


@pytest.mark.parametrize("where", ["ciphertext", "tag", "nonce"])
def test_a_tampered_ciphertext_nonce_or_tag_does_not_open(vault, where):
    sealed = vault.seal(SECRET, CONNECTION)
    flip = lambda data, i: data[:i] + bytes([data[i] ^ 1]) + data[i + 1 :]  # noqa: E731
    changed = {
        "ciphertext": Sealed(flip(sealed.ciphertext, 0), sealed.nonce, sealed.key_id, sealed.version),
        "tag": Sealed(flip(sealed.ciphertext, -1), sealed.nonce, sealed.key_id, sealed.version),
        "nonce": Sealed(sealed.ciphertext, flip(sealed.nonce, 3), sealed.key_id, sealed.version),
    }[where]
    assert code(vault.open, changed, CONNECTION) == "decrypt_failed"


@pytest.mark.parametrize(
    "sealed",
    [
        Sealed(b"short", b"\x00" * 12, "k1", 1),
        Sealed(b"x" * 40, b"\x00" * 5, "k1", 1),
        Sealed(b"x" * 40, b"\x00" * 12, "k1", 2),
        Sealed(b"x" * 40, b"\x00" * 12, "bad id!", 1),
        Sealed(b"", b"", "", 0),
    ],
)
def test_a_malformed_envelope_is_refused_before_any_decryption(vault, sealed):
    assert code(vault.open, sealed, CONNECTION) == "bad_envelope"


def test_the_error_never_carries_the_secret_a_key_or_the_ciphertext(vault, key_line):
    sealed = vault.seal(SECRET, CONNECTION)
    error = None
    try:
        vault.open(Sealed(sealed.ciphertext[:-1] + b"\x00", sealed.nonce, sealed.key_id, 1), CONNECTION)
    except VaultError as caught:
        error = caught
    text = repr(error) + str(error) + repr(error.__cause__) + repr(error.__context__ or "")
    assert key_line.partition(":")[2] not in text and SECRET not in text and sealed.ciphertext.hex() not in text


# ---------- the key ring: missing, damaged, several keys ----------


def test_a_missing_key_is_a_fixed_word_not_a_guess():
    class Missing(Exception):
        code = "missing"

    def load():
        raise Missing("secret path C:\\secret\\encryption.key")

    assert code(Vault(load).seal, SECRET, CONNECTION) == "key_missing"
    assert code(Vault(load).open, Sealed(b"x" * 40, b"\x00" * 12, "k1", 1), CONNECTION) == "key_missing"
    assert Vault(load).has_key("k1") is False


def test_a_key_that_cannot_be_read_for_another_reason_is_unavailable_and_says_nothing_else():
    def load():
        raise OSError("C:\\Users\\x\\secrets\\encryption.key: access denied")

    error = None
    try:
        Vault(load).seal(SECRET, CONNECTION)
    except VaultError as caught:
        error = caught
    assert error.code == "key_unavailable" and "secrets" not in repr(error) and error.__cause__ is None


@pytest.mark.parametrize(
    "text",
    [
        "",
        "\n\n",
        "# only a comment\n",
        "k1",
        "k1:",
        "k1:!!!notbase64",
        "k1:" + "A" * 10,
        "bad id:" + "A" * 43,
        ":" + "A" * 43,
        new_key_line("d") + "\n" + new_key_line("d"),
    ],
)
def test_a_damaged_key_ring_is_refused(text):
    assert code(parse_keyring, text) == "key_invalid"


def test_the_first_line_seals_and_every_line_opens():
    old, new = new_key_line("old"), new_key_line("new")
    sealed_old = Vault(lambda: ring(old)).seal(SECRET, CONNECTION)
    rotated = Vault(lambda: ring(new, old))  # a new key added on top: rotation without a flag day
    assert rotated.active_key_id() == "new" and rotated.key_ids() == {"new", "old"}
    assert rotated.open(sealed_old, CONNECTION) == SECRET  # the old one still opens
    resealed = rotated.seal(rotated.open(sealed_old, CONNECTION), CONNECTION)
    assert resealed.key_id == "new" and rotated.open(resealed, CONNECTION) == SECRET
    assert code(Vault(lambda: ring(new)).open, sealed_old, CONNECTION) == "unknown_key"  # the old key removed: closed


def test_comments_and_blank_lines_in_the_ring_are_ignored():
    line = new_key_line("k9")
    assert parse_keyring(f"# the ring\n\n{line}\n")[0] == "k9"


def test_the_key_is_read_again_on_every_call_so_a_removed_key_is_gone_at_once():
    state = {"text": ring(new_key_line("k1"))}
    vault = Vault(lambda: state["text"])
    sealed = vault.seal(SECRET, CONNECTION)
    state["text"] = ring(new_key_line("k2"))
    assert code(vault.open, sealed, CONNECTION) == "unknown_key"


# ---------- what may be sealed ----------


@pytest.mark.parametrize("value", ["", None, 123, b"bytes"])
def test_only_a_non_empty_text_is_sealed(vault, value):
    assert code(vault.seal, value, CONNECTION) == "empty"


def test_a_secret_longer_than_a_password_can_be_is_refused(vault):
    assert code(vault.seal, "x" * (MAX_SECRET_CHARS + 1), CONNECTION) == "too_long"
    assert vault.open(vault.seal("x" * MAX_SECRET_CHARS, CONNECTION), CONNECTION) == "x" * MAX_SECRET_CHARS


def test_without_the_cryptography_library_the_vault_says_so_and_does_nothing(vault, monkeypatch):
    import builtins

    real = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name.startswith("cryptography"):
            raise ImportError(name)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    assert code(vault.seal, SECRET, CONNECTION) == "crypto_unavailable"


def test_a_connection_id_that_is_not_an_id_is_not_sealed_for(vault):
    with pytest.raises(ValueError):
        vault.seal(SECRET, "not-a-uuid")
    assert os.urandom  # (the nonce comes from the operating system's generator)
