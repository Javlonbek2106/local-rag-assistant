"""Where the connector meets the rest of the application (the only place that knows both sides, besides rag/api.py).

The connector package imports nothing of the authentication layer: here the principal and the step-up check of the
authentication context are handed to it as plain callables, and the encryption key is read through the private-secret
reader the application already has (rag/auth/secretfiles) and given to the vault as text.
"""

from rag import config
from rag.auth import secretfiles
from rag.connector import api as connector_api
from rag.connector.service import Service
from rag.connector.vault import Vault


def read_key_ring():
    """The key ring text from the private secret directory (outside PostgreSQL, the backups, the repository and the
    image). SecretError(code) when it is missing or not private enough: the vault turns that into one fixed word."""
    return secretfiles.read_secret(config.ENCRYPTION_KEY_FILE)


def build_service(events):
    return Service(Vault(read_key_ring), events)


def install(app, security):
    """Add /api/v1/connections to `app`; `security` is the AuthContext (rag.auth.deps) of the connector."""
    connector_api.install(
        app, build_service(security.events), principal=security.current_principal, step_up=security.require_step_up
    )
