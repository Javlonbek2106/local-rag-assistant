"""M4.1b and the backups (roadmap 6a.11 criteria 4 and 14): a backup holds the ENCRYPTED credentials and never the key
or a plaintext password; a restored system opens its credentials only with the key; without it the connections say
`needs_credentials` and nothing else is affected.

Real PostgreSQL and Qdrant in throw-away Docker containers (tests/stack.py). Needs RAG_INTEGRATION=1 and Docker.
"""

import subprocess

import pytest

pytest.importorskip("cryptography")

from qdrant_client import models  # noqa: E402

from rag import backup, config, db, store  # noqa: E402
from rag.auth import secretfiles  # noqa: E402
from rag.auth.principals import Principals  # noqa: E402
from rag.backup import PgTools  # noqa: E402
from rag.connector import repository  # noqa: E402
from rag.connector.service import Service  # noqa: E402
from rag.connector.vault import Vault, new_key_line  # noqa: E402
from rag.connector_wiring import read_key_ring  # noqa: E402
from rag.target import Target  # noqa: E402
from tests import stack as stack_tools  # noqa: E402
from tests.auth_support import Events  # noqa: E402
from tests.connector_support import PASSWORD, PAYLOAD, FakeDriver, internal, resolver  # noqa: E402

pytestmark = pytest.mark.integration


@pytest.fixture
def stacks():
    if not stack_tools.docker_available():
        pytest.skip("Docker is not available")
    source, target = stack_tools.Stack("cbs").start(), stack_tools.Stack("cbt").start()
    yield source, target
    source.stop()
    target.stop()


def service_for(events=None):
    return Service(
        Vault(read_key_ring),
        events or Events(),
        resolver=resolver(),
        internal=internal,
        driver_lookup=FakeDriver(),
    )


def test_a_backup_holds_the_ciphertext_not_the_key_and_a_restore_opens_it_only_with_the_key(
    stacks, tmp_path, monkeypatch
):
    source, target_stack = stacks
    secrets = tmp_path / "secrets"
    monkeypatch.setattr(config, "SECRETS_DIR", secrets)
    monkeypatch.setattr(config, "DB_HARDENED", False)
    monkeypatch.setattr(config, "DB_SCHEMA", None)
    ring = new_key_line("k1")
    secretfiles.write_secret(config.ENCRYPTION_KEY_FILE, ring + "\n")

    # ---- the source system: one connection with a password, a collection and a storage folder
    storage = tmp_path / "source-storage"
    storage.mkdir()
    monkeypatch.setattr(config, "DATABASE_URL", source.database_url)
    db.close()
    db.migrate()
    owner = Principals().local_owner()["id"]
    made = service_for().create(owner, PAYLOAD)
    sealed_before = repository.get_sealed(owner, made["id"])
    client = Target(source.database_url, source.qdrant_url, "src_docs", storage).qdrant()
    client.create_collection("src_docs", vectors_config=models.VectorParams(size=4, distance=models.Distance.COSINE))
    for field in store.INDEXED_FIELDS:
        client.create_payload_index("src_docs", field, models.PayloadSchemaType.KEYWORD)
    folder = backup.backup(
        Target(source.database_url, source.qdrant_url, "src_docs", storage),
        tmp_path / "backups",
        PgTools(container=source.pg_container),
    )
    db.close()

    # ---- what the backup holds: the SQL of the dump has the encrypted credential and neither the password nor the key
    text = subprocess.run(
        ["docker", "exec", "-i", source.pg_container, "pg_restore", "--no-owner", "-f", "-"],
        input=(folder / "postgres.dump").read_bytes(),
        capture_output=True,
    ).stdout.decode("utf-8", "replace")
    assert "COPY public.connections" in text and made["id"] in text
    assert sealed_before.ciphertext.hex() in text  # the ciphertext is there ...
    assert PASSWORD not in text and ring.partition(":")[2] not in text  # ... the password and the key are not
    everything = b"".join(f.read_bytes() for f in folder.rglob("manifest.json"))
    assert PASSWORD.encode() not in everything and ring.partition(":")[2].encode() not in everything

    # ---- a restore into a new system: the rows come back, the key does not (it was never in the backup)
    restored = Target(
        target_stack.database_url, target_stack.qdrant_url, "restored_docs", tmp_path / "restored-storage"
    )
    backup.restore(folder, restored, PgTools(container=target_stack.pg_container))
    monkeypatch.setattr(config, "DATABASE_URL", target_stack.database_url)
    db.close()
    try:
        secretfiles.write_secret(
            config.ENCRYPTION_KEY_FILE, new_key_line("another") + "\n"
        )  # a machine with another key
        lost = service_for().get(owner, made["id"])
        assert (
            lost["status"] == "needs_credentials" and lost["name"] == "Sales"
        )  # the connection is there; its secret is not
        with pytest.raises(Exception) as refused:
            service_for().test(owner, made["id"])
        assert getattr(refused.value, "code", "") == "needs_credentials"
        assert db.get_document("nothing-here") is None  # documents are not affected by any of it

        # ---- with the key the credential opens and the connection works again; `connector verify` agrees
        secretfiles.write_secret(config.ENCRYPTION_KEY_FILE, ring + "\n")
        found = service_for().get(owner, made["id"])
        assert found["status"] == "ready" and service_for().test(owner, made["id"])["status"] == "ok"
        assert repository.get_sealed(owner, made["id"]) == sealed_before  # the very same ciphertext
        from rag import connectorcli

        assert connectorcli.verify(Vault(read_key_ring)) == 0
    finally:
        db.close()
