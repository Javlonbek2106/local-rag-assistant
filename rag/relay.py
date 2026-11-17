"""Run blocking work (retrieval and the LLM's streamed answer) in a thread of its own and hand its output to async code.

Why not the thread pool that FastAPI uses for a plain `def` endpoint: when the client goes away, nothing can stop a pool
thread that is blocked waiting for Ollama, so the generation ran on (for nobody) and every later question waited behind
it. A Relay owns its thread and a `llm.Scope`; `cancel()` shuts the thread's Ollama connection, which wakes it at once,
and Ollama stops generating when it sees the connection close.
"""
import asyncio
import logging
import threading

from rag import llm

log = logging.getLogger(__name__)


class Relay:
    """`produce()` returns an iterator (it is started in the thread and closed there when the relay stops). Items
    come out of `get()` as ("item", value), then either ("end", None) or ("error", exception). A cancelled relay ends
    quietly."""

    def __init__(self, produce):
        self.scope = llm.Scope()
        self._produce = produce
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True, name="relay")

    def start(self):
        self._thread.start()
        return self

    def _put(self, kind, value=None):
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, (kind, value))
        except RuntimeError:  # the event loop is already closed (shutdown): nobody is listening
            pass

    def _run(self):
        try:
            with llm.bind(self.scope):
                items = None
                try:
                    items = self._produce()
                    for item in items:
                        if self.scope.cancelled:
                            break
                        self._put("item", item)
                finally:
                    if items is not None and hasattr(items, "close"):
                        items.close()  # runs the generators' cleanup, which closes the connection to Ollama
        except llm.LLMCancelled:
            pass
        except BaseException as error:  # noqa: BLE001 - everything the work raises goes to the caller
            if not self.scope.cancelled:
                self._put("error", error)
        finally:
            log.info("relay thread finished (cancelled=%s)", self.scope.cancelled)
            self._put("end")

    async def get(self, timeout):
        """The next (kind, value), or None if nothing arrived within `timeout` seconds."""
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except TimeoutError:
            return None

    def cancel(self):
        self.scope.cancel()
