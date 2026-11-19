"""The local Ollama server: embeddings and the streamed answer.

Plain HTTP (http.client), not the `ollama` package, for three things the package cannot do:

- Cancellation. A question whose client has gone away must stop the generation at once (Ollama keeps generating for
  nobody otherwise, and everything behind it waits). A `Scope` collects the sockets of the calls made while it is bound
  to a thread; `Scope.cancel()` from another thread shuts them down, which wakes the blocked reader immediately, and
  Ollama stops generating as soon as it sees the connection close.
- Timeouts that tell "slow" from "dead". Ollama only starts its response with the first token, which can take over
  30 s (loading the model, reading the prompt), so a plain read timeout would kill good requests. While waiting, a
  cheap /api/version request asks whether Ollama is alive at all (it answers within milliseconds even while busy);
  no sign of life for OLLAMA_UNRESPONSIVE_S is an error, a live but slow answer may take up to the long limits.
- Limits on the output and on how long a model stays loaded (num_predict, keep_alive).

Errors are LLMUnavailable (not running, or not answering), LLMTimeout (alive but too slow), LLMCancelled (the caller
went away) and LLMError (anything else Ollama reports); the API turns them into 503 / 504 / nothing / 503.
"""
import http.client
import json
import logging
import os
import re
import select
import socket
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

from rag import config

log = logging.getLogger(__name__)
HEADERS = {"Content-Type": "application/json"}


class LLMError(Exception):
    """Ollama refused or failed the request (for example the model is not installed)."""


class LLMUnavailable(LLMError):
    """Ollama is not running, or does not answer."""


class LLMTimeout(LLMError):
    """Ollama is alive but did not produce the answer in time."""


class LLMCancelled(LLMError):
    """The caller went away; the work was stopped on purpose."""


def ollama_base():
    host = os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434"
    host = host.rstrip("/")
    if "://" not in host:
        host = f"http://{host}"
    if not re.search(r":\d+$", host):
        host += ":11434"
    return host


# ---------- cancellation ----------

class Scope:
    """What one request (a question) is doing with Ollama, so that another thread can stop it.

    Bind it to the thread that does the work (`with bind(scope):`); every call that thread makes registers its socket
    here. `cancel()` may be called from any thread, any number of times. `truncated` is set when the last answer
    stopped at the num_predict limit instead of finishing.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._sockets = []
        self.cancelled = False
        self.truncated = False

    def attach(self, sock):
        with self._lock:
            if self.cancelled:
                raise LLMCancelled("so'rov bekor qilindi")
            self._sockets.append(sock)

    def detach(self, sock):
        with self._lock:
            if sock in self._sockets:
                self._sockets.remove(sock)

    def cancel(self):
        with self._lock:
            self.cancelled = True
            sockets = list(self._sockets)
        log.info("cancelling a request: %d open connection(s) to Ollama", len(sockets))
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)  # wakes a reader blocked on it; its owner closes it
            except OSError:
                pass


_bound = threading.local()


@contextmanager
def bind(scope):
    """Make `scope` the one calls made by this thread register with."""
    previous = getattr(_bound, "scope", None)
    _bound.scope = scope
    try:
        yield scope
    finally:
        _bound.scope = previous


def _scope():
    return getattr(_bound, "scope", None)


# ---------- one HTTP call ----------

def _connect(timeout):
    parts = urlsplit(ollama_base())
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)
    try:
        conn.connect()
    except OSError as error:
        raise LLMUnavailable(f"Ollama bilan aloqa yo'q ({ollama_base()}). Ollama ishlayaptimi?") from error
    scope = _scope()
    if scope:
        try:
            scope.attach(conn.sock)
        except LLMCancelled:
            conn.close()
            raise
    return conn


def _close(conn):
    scope = _scope()
    if scope and conn.sock:
        scope.detach(conn.sock)
    conn.close()


def _alive():
    """Does Ollama answer a trivial request right now? (It does, within milliseconds, even while generating.)"""
    try:
        conn = _connect(min(config.OLLAMA_PROBE_INTERVAL_S, config.OLLAMA_CONNECT_TIMEOUT_S))
    except LLMUnavailable:
        return False
    try:
        conn.request("GET", "/api/version")
        return conn.getresponse().status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        _close(conn)


def _await_start(conn, limit_s):
    """Wait until Ollama starts answering `conn`'s request. Raises LLMUnavailable when there is no sign of life for
    OLLAMA_UNRESPONSIVE_S, LLMTimeout after `limit_s` of a live but slow Ollama, LLMCancelled if the scope is
    cancelled."""
    scope = _scope()
    started = last_alive = time.monotonic()
    while True:
        now = time.monotonic()
        if now - started >= limit_s:
            raise LLMTimeout(f"Ollama {limit_s:g} soniyada javob bermadi (model juda sekin ishlayapti)")
        try:
            wait = min(config.OLLAMA_PROBE_INTERVAL_S, limit_s - (now - started))
            ready, _, _ = select.select([conn.sock], [], [], wait)
        except (OSError, ValueError):
            ready = [conn.sock]  # the socket was shut down under us: reading it will say why
        if scope and scope.cancelled:
            raise LLMCancelled("so'rov bekor qilindi")
        if ready:
            return
        if _alive():
            last_alive = time.monotonic()
        elif time.monotonic() - last_alive >= config.OLLAMA_UNRESPONSIVE_S:
            raise LLMUnavailable(f"Ollama {config.OLLAMA_UNRESPONSIVE_S:g} soniyadan beri hech qanday javob bermayapti")


def _start(path, payload, limit_s):
    """Send a POST and wait for the response to start. Returns (connection, response); the caller closes it."""
    conn = _connect(config.OLLAMA_CONNECT_TIMEOUT_S)
    try:
        conn.request("POST", path, json.dumps(payload), HEADERS)
        _await_start(conn, limit_s)
        conn.sock.settimeout(config.LLM_IDLE_TIMEOUT_S)
        response = conn.getresponse()
        if response.status != 200:
            raise LLMError(_error_text(response))
        return conn, response
    except BaseException as error:
        _close(conn)
        if isinstance(error, (OSError, http.client.HTTPException)) and not isinstance(error, LLMError):
            raise _transport_error(error) from error
        raise


def _transport_error(error):
    scope = _scope()
    if scope and scope.cancelled:
        return LLMCancelled("so'rov bekor qilindi")
    if isinstance(error, TimeoutError):
        return LLMTimeout("Ollama javobni yuborishni to'xtatdi")
    return LLMUnavailable(f"Ollama ulanishni uzdi ({type(error).__name__})")


def _error_text(response):
    try:
        body = json.loads(response.read() or b"{}")
        message = body.get("error") if isinstance(body, dict) else None
    except (ValueError, OSError, http.client.HTTPException):
        message = None
    return f"Ollama xatosi (HTTP {response.status}): {message or response.reason}"


def _call(path, payload, limit_s):
    """A request with one JSON answer."""
    conn, response = _start(path, payload, limit_s)
    try:
        conn.sock.settimeout(limit_s)
        return json.loads(response.read())
    except (OSError, http.client.HTTPException) as error:
        raise _transport_error(error) from error
    finally:
        _close(conn)


# ---------- what the rest of the program uses ----------

def embed(texts):
    vectors = []
    for i in range(0, len(texts), config.EMBED_BATCH):
        answer = _call("/api/embed", {"model": config.EMBED_MODEL, "input": texts[i : i + config.EMBED_BATCH],
                                      "keep_alive": config.OLLAMA_KEEP_ALIVE}, config.EMBED_TIMEOUT_S)
        vectors += answer["embeddings"]
    return vectors


def chat_stream(messages):
    """Yield the answer text piece by piece.

    Ends when the model finishes or reaches the output limit (then the bound scope's `truncated` is set). Closing the
    generator, or cancelling the bound scope, closes the connection, which makes Ollama stop generating.
    """
    payload = {"model": config.LLM_MODEL, "messages": messages, "stream": True, "keep_alive": config.OLLAMA_KEEP_ALIVE,
               "options": {"temperature": 0, "num_ctx": config.LLM_NUM_CTX, "num_predict": config.LLM_NUM_PREDICT}}
    scope = _scope()
    started = time.monotonic()
    # Ollama starts its response with the first token, so this is also the first-token wait
    conn, response = _start("/api/chat", payload, config.LLM_FIRST_TOKEN_TIMEOUT_S)
    try:
        while True:
            try:
                line = response.readline()
            except (OSError, http.client.HTTPException) as error:
                raise _transport_error(error) from error
            if not line:  # the connection ended without "done"
                raise (LLMCancelled("so'rov bekor qilindi") if scope and scope.cancelled
                       else LLMUnavailable("Ollama javobni to'liq yubormasdan ulanishni uzdi"))
            data = json.loads(line)
            if data.get("error"):
                raise LLMError(f"Ollama xatosi: {data['error']}")
            piece = (data.get("message") or {}).get("content")
            if piece:
                yield piece
            if data.get("done"):
                if scope:
                    scope.truncated = data.get("done_reason") == "length"
                return
            if time.monotonic() - started > config.LLM_TOTAL_TIMEOUT_S:
                raise LLMTimeout(f"Javob {config.LLM_TOTAL_TIMEOUT_S:g} soniyada tugamadi")
    finally:
        _close(conn)


def warm_up():
    """Load both models and keep them loaded, so the first question does not wait for it. A failure only means the first
    question is slower (or reports Ollama's problem itself), so it is logged and not raised."""
    for path, payload in (
        ("/api/embed", {"model": config.EMBED_MODEL, "input": "isitish", "keep_alive": config.OLLAMA_KEEP_ALIVE}),
        ("/api/generate", {"model": config.LLM_MODEL, "keep_alive": config.OLLAMA_KEEP_ALIVE, "stream": False}),
    ):
        try:
            _call(path, payload, config.LLM_FIRST_TOKEN_TIMEOUT_S)
            log.info("warm-up: %s loaded", payload["model"])
        except LLMError as error:
            log.warning("warm-up of %s failed: %s", payload["model"], error)
