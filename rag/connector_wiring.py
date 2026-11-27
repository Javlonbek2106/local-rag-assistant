"""Where the connector meets the rest of the application (the only place that knows both sides, besides rag/api.py).

The connector package imports nothing of the authentication layer: here the principal and the step-up check of the
authentication context are handed to it as plain callables, and the encryption key is read through the private-secret
reader the application already has (rag/auth/secretfiles) and given to the vault as text.
"""

import asyncio
import logging

from rag import config, sqlmodel
from rag.auth import secretfiles
from rag.connector import api as connector_api
from rag.connector.service import Service
from rag.connector.vault import Vault
from rag.relay import Relay

log = logging.getLogger(__name__)


def read_key_ring():
    """The key ring text from the private secret directory (outside PostgreSQL, the backups, the repository and the
    image). SecretError(code) when it is missing or not private enough: the vault turns that into one fixed word."""
    return secretfiles.read_secret(config.ENCRYPTION_KEY_FILE)


def build_service(events):
    return Service(Vault(read_key_ring), events, sql_completer=sqlmodel.complete)


async def run_cancellable(request, work, cancel=None):
    """Run `work()` (the SQL generation: some database reads and one call to the model) in a thread that has an Ollama
    cancellation scope, and wait for it while watching the client. Returns what `work` returns, raises what it raises,
    and returns None when the client went away first: the scope is then cancelled, which closes the connection to
    Ollama and frees the model within moments (the same relay the question answerer uses). `cancel` (a
    drivers.CancelToken)
    is also cancelled then: a query that is running on the external database is stopped as well."""
    relay = Relay(lambda: iter([work()])).start()
    gone = asyncio.Event()

    async def watch_client():
        while (await request.receive())["type"] != "http.disconnect":
            pass
        gone.set()

    watcher = asyncio.create_task(watch_client())
    outcome = None
    try:
        while True:
            event = await relay.get(0.25)
            if event is None:
                if gone.is_set():
                    relay.cancel()
                    if cancel:
                        cancel.cancel()
                    log.info("sql generation: the client went away; stopped")
                    return None
                continue
            kind, value = event
            if kind == "item":
                outcome = value
            elif kind == "error":
                raise value
            elif kind == "end":
                return outcome
    except BaseException:
        relay.cancel()
        if cancel:
            cancel.cancel()
        raise
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


def install(app, security):
    """Add /api/v1/connections to `app`; `security` is the AuthContext (rag.auth.deps) of the connector."""
    connector_api.install(
        app,
        build_service(security.events),
        principal=security.current_principal,
        step_up=security.require_step_up,
        runner=run_cancellable,
    )
