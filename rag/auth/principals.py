"""The principals of this installation (migration 0005). For now exactly one: the local owner.

`local_owner()` is also the start-up check of that guarantee: no row, two rows or a disabled one is a failure of the
authentication layer, and the connector stays unavailable (it never guesses an owner).
"""
from rag import config, db

LOCAL_OWNER = "local-owner"


class PrincipalError(Exception):
    """The local owner cannot be established. `code` is a fixed word, never a value from the database."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class Principals:
    """Reads the principals through the application's own pool (the `db` module)."""

    def local_owner(self):
        try:
            with db.pool().connection(timeout=config.SECURITY_DB_TIMEOUT_S) as conn:
                rows = conn.execute("SELECT id, kind, display_label, user_id, claimed_at, disabled_at FROM principals "
                                    "WHERE kind = %s", (LOCAL_OWNER,)).fetchall()
        except Exception as error:  # noqa: BLE001 - the reason is not for the caller: no connection text leaves here
            raise PrincipalError("unreachable") from error
        if len(rows) != 1:
            raise PrincipalError("not_exactly_one")
        row = rows[0]
        if row["disabled_at"] is not None:
            raise PrincipalError("disabled")
        return {"id": str(row["id"]), "kind": row["kind"]}
