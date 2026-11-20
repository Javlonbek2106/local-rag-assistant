"""rag/sqlmodel.py, the language model of the SQL generator, against a fake Ollama speaking real HTTP: the settings it
sends (its own model, window, output limit, JSON), what it returns, how each kind of failure becomes a fixed code, and
that the cancellation scope of the request is the one that is used. Plus the runner that watches the client."""

import asyncio
import threading
import time

import pytest

from rag import config, llm, sqlmodel
from rag.connector.errors import ModelError
from tests.fake_ollama import FakeOllama

MESSAGES = [{"role": "system", "content": "rules"}, {"role": "user", "content": "question"}]
ANSWER = '{"sql": "SELECT 1", "explanation": "one", "assumptions": [], "ambiguities": []}'


@pytest.fixture
def ollama(monkeypatch):
    fake = FakeOllama().start()
    fake.tokens = [ANSWER[:20], ANSWER[20:]]
    monkeypatch.setenv("OLLAMA_HOST", fake.url)
    monkeypatch.setattr(config, "OLLAMA_PROBE_INTERVAL_S", 0.1)
    monkeypatch.setattr(config, "OLLAMA_UNRESPONSIVE_S", 0.5)
    monkeypatch.setattr(config, "OLLAMA_CONNECT_TIMEOUT_S", 1)
    yield fake
    fake.stop()


def code_of(call, *args):
    with pytest.raises(ModelError) as error:
        call(*args)
    return error.value.code


def test_the_request_uses_the_sql_model_window_output_limit_and_asks_for_json(ollama, monkeypatch):
    monkeypatch.setattr(config, "SQL_MODEL", "sql-model:1b")
    monkeypatch.setattr(config, "SQL_NUM_CTX", 4096)
    monkeypatch.setattr(config, "SQL_NUM_PREDICT", 321)
    done = sqlmodel.complete(MESSAGES)
    assert (done.text, done.model, done.truncated) == (ANSWER, "sql-model:1b", False)
    (sent,) = ollama.chats()
    assert sent["model"] == "sql-model:1b" and sent["format"] == "json" and sent["messages"] == MESSAGES
    assert sent["options"] == {"temperature": 0, "num_ctx": 4096, "num_predict": 321}


def test_the_answer_settings_of_the_question_answerer_are_untouched(ollama, monkeypatch):
    monkeypatch.setattr(config, "LLM_MODEL", "answer-model")
    monkeypatch.setattr(config, "LLM_NUM_PREDICT", 99)
    list(llm.chat_stream(MESSAGES))
    (sent,) = ollama.chats()
    assert sent["model"] == "answer-model" and "format" not in sent and sent["options"]["num_predict"] == 99


def test_the_sql_model_defaults_to_the_answer_model():
    assert config.SQL_MODEL == (config.os.getenv("RAG_SQL_MODEL") or config.LLM_MODEL)


def test_an_answer_cut_at_the_output_limit_is_reported_as_truncated(ollama):
    ollama.done_reason = "length"
    assert sqlmodel.complete(MESSAGES).truncated is True
    ollama.done_reason = "stop"
    assert sqlmodel.complete(MESSAGES).truncated is False


def test_a_model_that_is_not_there_is_a_fixed_code_and_none_of_ollamas_text(ollama):
    ollama.chat_status = 404  # "model 'x' not found"
    with pytest.raises(ModelError) as error:
        sqlmodel.complete(MESSAGES)
    assert error.value.code == "llm_error" and "model" not in str(error.value) and error.value.__cause__ is None


def test_a_crash_in_the_middle_is_llm_unavailable_or_error_never_a_partial_answer(ollama):
    ollama.error_after = 1
    assert code_of(sqlmodel.complete, MESSAGES) == "llm_error"


def test_ollama_not_running_is_llm_unavailable(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:1")
    monkeypatch.setattr(config, "OLLAMA_CONNECT_TIMEOUT_S", 1)
    assert code_of(sqlmodel.complete, MESSAGES) == "llm_unavailable"


def test_a_model_that_is_too_slow_is_llm_timeout(ollama, monkeypatch):
    ollama.first_delay = 5
    monkeypatch.setattr(config, "LLM_FIRST_TOKEN_TIMEOUT_S", 0.4)
    assert code_of(sqlmodel.complete, MESSAGES) == "llm_timeout"


def test_the_scope_of_the_request_is_used_so_that_a_hung_up_client_stops_the_model(ollama):
    ollama.endless, ollama.token_delay = True, 0.02
    scope, outcome = llm.Scope(), []

    def work():
        with llm.bind(scope):
            try:
                sqlmodel.complete(MESSAGES)
            except ModelError as error:
                outcome.append(error.code)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    assert ollama.chat_started.wait(5)
    scope.cancel()
    thread.join(5)
    assert outcome == ["llm_cancelled"] and ollama.closed.wait(5)  # and Ollama saw the connection close


def test_without_a_scope_the_call_still_works(ollama):
    assert llm.current_scope() is None and sqlmodel.complete(MESSAGES).text == ANSWER


# ---------- the runner that watches the client (rag/connector_wiring.py) ----------


class FakeRequest:
    """Enough of a Starlette request for the runner: `receive()` gives messages until the client 'hangs up'."""

    def __init__(self):
        self.hangup = threading.Event()

    async def receive(self):
        while not self.hangup.is_set():
            await asyncio.sleep(0.01)
        return {"type": "http.disconnect"}


def run(work, request=None):
    from rag.connector_wiring import run_cancellable

    return asyncio.run(run_cancellable(request or FakeRequest(), work))


def test_the_runner_returns_what_the_work_returns_and_raises_what_it_raises():
    assert run(lambda: {"sql": "SELECT 1"}) == {"sql": "SELECT 1"}
    with pytest.raises(ValueError, match="boom"):
        run(lambda: (_ for _ in ()).throw(ValueError("boom")))


def test_the_work_runs_with_an_ollama_scope_bound_to_its_thread():
    assert run(lambda: llm.current_scope() is not None) is True


def test_a_client_that_goes_away_cancels_the_scope_and_the_runner_returns_none():
    request, seen, started = FakeRequest(), {}, threading.Event()

    def work():
        scope = llm.current_scope()
        started.set()
        end = time.monotonic() + 5
        while not scope.cancelled and time.monotonic() < end:
            time.sleep(0.01)
        seen["cancelled"] = scope.cancelled
        return "late"

    threading.Timer(0.3, request.hangup.set).start()
    began = time.monotonic()
    assert run(work, request) is None
    assert started.is_set() and time.monotonic() - began < 3
    deadline = time.monotonic() + 3
    while "cancelled" not in seen and time.monotonic() < deadline:
        time.sleep(0.01)
    assert seen == {"cancelled": True}


# ---------- a real web server: a client that hangs up stops the model ----------


@pytest.fixture
def live_route(ollama):
    """The generation route behind a real uvicorn (with the same kind of middleware the application has), whose service
    asks the real sqlmodel.complete of the fake Ollama."""
    import socket
    from types import SimpleNamespace

    import uvicorn
    from fastapi import FastAPI

    from rag.connector import api as connector_api
    from rag.connector_wiring import run_cancellable

    class Service:
        def generate_sql(self, owner, connection_id, question):
            done = sqlmodel.complete(MESSAGES)
            return {"sql": done.text, "executed": False}

    app = FastAPI()
    app.include_router(
        connector_api.build_router(
            Service(), principal=lambda: SimpleNamespace(id="owner"), step_up=lambda: None, runner=run_cancellable
        )
    )

    @app.middleware("http")
    async def guard(request, call_next):  # a BaseHTTPMiddleware, as rag/api.py has: it wraps `receive`
        return await call_next(request)

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started
    yield port
    server.should_exit = True
    thread.join(10)


def post(port, body):
    import socket

    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    data = body.encode()
    head = "POST /api/v1/connections/abc/sql/generate HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
    sock.sendall(f"{head}Content-Length: {len(data)}\r\n\r\n".encode() + data)
    return sock


def test_a_client_that_hangs_up_while_the_model_is_writing_stops_the_model(ollama, live_route):
    ollama.endless, ollama.token_delay = True, 0.05
    sock = post(live_route, '{"question": "q"}')
    assert ollama.chat_started.wait(10)
    time.sleep(0.3)  # the model is mid-answer
    sock.close()  # the browser goes away
    assert ollama.closed.wait(10), "Ollama kept generating for nobody"


def test_a_client_that_waits_gets_the_answer_through_the_real_server(ollama, live_route):
    sock = post(live_route, '{"question": "q"}')
    sock.settimeout(15)
    data = b""
    while b"executed" not in data:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
    sock.close()
    assert data.startswith(b"HTTP/1.1 200") and b'"executed":false' in data.replace(b" ", b"")
