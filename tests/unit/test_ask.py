"""The ask endpoint against a real web server (uvicorn) and a fake Ollama: a client that disconnects frees Ollama at
once, the next question is not blocked behind a cancelled one, a dead or too slow Ollama gives a clear HTTP error, and
the output is bounded. The client is a raw socket, so "the browser closed the tab" is exactly that.
"""
import http.client
import json
import socket
import threading
import time

import pytest
import uvicorn

from rag import answer, config
from rag.api import create_app
from tests.fake_ollama import FakeOllama
from tests.helpers import make_hit


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def ollama(monkeypatch):
    fake = FakeOllama().start()
    monkeypatch.setenv("OLLAMA_HOST", fake.url)
    monkeypatch.setattr(config, "OLLAMA_PROBE_INTERVAL_S", 0.1)
    monkeypatch.setattr(config, "OLLAMA_UNRESPONSIVE_S", 0.5)
    monkeypatch.setattr(answer, "retrieve", lambda q, repo=None: [make_hit(0.8, "a.txt", "Lola-5G", [2])])
    yield fake
    fake.stop()


@pytest.fixture
def web(storage_dir, repo, qdrant, monkeypatch, ollama):
    """The real application on a real port; returns that port."""
    port = free_port()
    monkeypatch.setattr(config, "PORT", port)
    server = uvicorn.Server(uvicorn.Config(create_app(repo), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started
    yield port
    server.should_exit = True
    thread.join(10)


def post(port, question="Savol?", timeout=30):
    """A complete request/response: (status, headers, text)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.request("POST", "/api/ask", json.dumps({"question": question}), {"Content-Type": "application/json"})
    response = conn.getresponse()
    text = response.read().decode()
    conn.close()
    return response.status, text


def events(text):
    return [json.loads(line) for line in text.splitlines() if line]


def open_raw(port, question="Savol?"):
    """Send the request on a bare socket and return it, so the test decides when to hang up."""
    sock = socket.create_connection(("127.0.0.1", port))
    body = json.dumps({"question": question}).encode()
    head = (f"POST /api/ask HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n\r\n").encode()
    sock.sendall(head + body)
    return sock


def read_until(sock, marker, seconds=5):
    sock.settimeout(0.1)
    data, end = b"", time.monotonic() + seconds
    while marker not in data and time.monotonic() < end:
        try:
            data += sock.recv(4096)
        except TimeoutError:
            pass
    return data


# ---------- the answer itself is unchanged ----------

def test_an_answer_streams_and_ends_with_its_sources(web, ollama):
    status, text = post(web)
    lines = events(text)
    assert status == 200
    assert "".join(e["token"] for e in lines[:-1]) == "Lola-5G [1]"
    assert lines[-1]["found"] is True and lines[-1]["final"] == "Lola-5G [1]" and "truncated" not in lines[-1]
    assert lines[-1]["sources"][0]["file"] == "a.txt"


def test_the_request_to_ollama_limits_the_output_and_keeps_the_model_loaded(web, ollama):
    post(web)
    [chat] = ollama.chats()
    assert chat["options"]["num_predict"] == config.LLM_NUM_PREDICT and chat["keep_alive"] == config.OLLAMA_KEEP_ALIVE


def test_an_answer_that_hit_the_output_limit_says_so(web, ollama):
    ollama.done_reason = "length"
    final = events(post(web)[1])[-1]
    assert final["truncated"] is True and final["found"] is True


# ---------- the client goes away ----------

def test_a_client_that_disconnects_mid_answer_frees_ollama_within_2_seconds(web, ollama):
    ollama.endless, ollama.token_delay = True, 0.05  # a generation that would run for ever
    sock = open_raw(web)
    assert b'"token"' in read_until(sock, b'"token"')  # the answer is flowing
    closed_by_client = time.monotonic()
    sock.close()
    assert ollama.closed.wait(2), "Ollama was never told the client went away"
    assert ollama.closed_at - closed_by_client < 2
    assert ollama.chat_finished.wait(1)  # the generation really ended


def test_a_client_that_disconnects_before_the_first_token_frees_ollama_within_2_seconds(web, ollama):
    ollama.first_delay = 60  # the model is still reading the prompt
    sock = open_raw(web)
    assert ollama.chat_started.wait(5)
    time.sleep(0.3)
    closed_by_client = time.monotonic()
    sock.close()
    assert ollama.closed.wait(3), "Ollama was never told the client went away"
    assert ollama.closed_at - closed_by_client < 2


def test_a_client_that_disconnects_while_the_model_is_silent_mid_answer_frees_ollama_within_2_seconds(web, ollama,
                                                                                                 monkeypatch):
    """No token is flowing, so nothing is being written to the client: the hang-up must be noticed on its own."""
    monkeypatch.setattr(config, "LLM_IDLE_TIMEOUT_S", 60)
    ollama.stall_after = 1  # one word, then silence
    sock = open_raw(web)
    assert b'"token"' in read_until(sock, b'"token"')
    time.sleep(0.3)
    closed_by_client = time.monotonic()
    sock.close()
    assert ollama.closed.wait(3), "Ollama was never told the client went away"
    assert ollama.closed_at - closed_by_client < 2


@pytest.mark.parametrize("silent", [False, True])
def test_the_hang_up_is_acted_on_at_once_without_waiting_for_garbage_collection(web, ollama, monkeypatch, silent):
    """Stopping the generation must not depend on when Python happens to finalize the response generator (a rare
    late collection once left an abandoned answer running): the garbage collector is off for this test."""
    import gc

    monkeypatch.setattr(config, "LLM_IDLE_TIMEOUT_S", 60)
    if silent:
        ollama.stall_after = 1
    else:
        ollama.endless, ollama.token_delay = True, 0.05
    gc.collect()
    gc.disable()
    try:
        for _ in range(3):
            ollama.closed.clear()
            ollama.closed_at = None
            sock = open_raw(web)
            assert b'"token"' in read_until(sock, b'"token"')
            closed_by_client = time.monotonic()
            sock.close()
            assert ollama.closed.wait(3), "Ollama was never told the client went away"
            assert ollama.closed_at - closed_by_client < 2
    finally:
        gc.enable()


@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
def test_a_cancelled_response_runs_its_cleanup_without_waiting_for_its_generator_to_be_finalized(spec_version):
    """The cause of a rare failure (about one full run in 20): the client hung up, the task sending the response was
    cancelled while the body generator sat suspended at a `yield`, and the generator's own `finally` (which stops the
    generation) only ran when the garbage collector got to it. The response itself must do the cleanup."""
    import asyncio
    import gc

    from rag.api import OwnedStream

    async def scenario():
        closed, finalized = [], []

        async def body():
            try:
                yield b"first"
                yield b"second"
            finally:
                finalized.append(True)

        generator = body()  # kept alive on purpose, as a reference cycle in a real server would keep it
        forever = asyncio.Event()

        async def send(message):
            if message["type"] == "http.response.body":
                await forever.wait()  # a client that no longer takes data

        async def receive():
            await forever.wait()

        response = OwnedStream(generator, on_close=lambda: closed.append(True))
        task = asyncio.create_task(response({"type": "http", "asgi": {"spec_version": spec_version}}, receive, send))
        await asyncio.sleep(0.1)
        task.cancel()  # what happens when the client disconnects
        try:
            await task
        except asyncio.CancelledError:
            pass
        return list(closed), list(finalized), generator  # copies: shutting the loop down closes the generator

    gc.disable()
    try:
        closed, finalized, _kept = asyncio.run(scenario())
    finally:
        gc.enable()
    assert closed == [True]  # the cleanup ran although the generator was never closed
    assert finalized == []  # (which is exactly the situation the old code depended on)


def test_the_next_question_is_not_blocked_by_a_cancelled_one(web, ollama):
    assert ollama.serial  # like the real Ollama on this machine: one answer at a time
    ollama.endless, ollama.token_delay = True, 0.05
    sock = open_raw(web, "birinchi savol")
    read_until(sock, b'"token"')
    sock.close()
    assert ollama.closed.wait(2)
    ollama.endless = False  # the next question gets a normal, finite answer
    started = time.monotonic()
    status, text = post(web, "ikkinchi savol", timeout=10)
    assert status == 200 and events(text)[-1]["found"] is True
    assert time.monotonic() - started < 2  # it did not wait for the abandoned generation to finish


def test_without_a_cancel_the_second_question_would_have_waited(web, ollama):
    """The control for the test above: the fake really is serial, so a generation that is not stopped does block."""
    ollama.endless, ollama.token_delay = True, 0.05
    first = open_raw(web, "birinchi")
    read_until(first, b'"token"')
    second = open_raw(web, "ikkinchi")
    assert b'"token"' not in read_until(second, b'"token"', seconds=1)  # stuck behind the first, still running
    first.close()  # now the first goes away: the second can start
    assert b'"token"' in read_until(second, b'"token"', seconds=5)
    second.close()


def test_cancelled_questions_leave_no_threads_behind(web, ollama):
    ollama.endless, ollama.token_delay = True, 0.05
    for _ in range(3):
        sock = open_raw(web)
        read_until(sock, b'"token"')
        sock.close()
        assert ollama.closed.wait(2)
        ollama.closed.clear()
        ollama.closed_at = None
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and any(t.name == "relay" for t in threading.enumerate()):
        time.sleep(0.05)
    assert not [t for t in threading.enumerate() if t.name == "relay"]


# ---------- Ollama does not answer: a clear error through the API ----------

def test_ollama_that_hangs_gives_a_503_with_a_code_within_the_limit(web, ollama):
    ollama.hang_everything = True
    started = time.monotonic()
    status, text = post(web)
    body = json.loads(text)
    assert status == 503 and body["code"] == "llm_unavailable" and "javob bermayapti" in body["detail"]
    assert time.monotonic() - started < config.OLLAMA_UNRESPONSIVE_S + 3


def test_ollama_that_is_alive_but_too_slow_gives_a_504(web, ollama, monkeypatch):
    monkeypatch.setattr(config, "LLM_FIRST_TOKEN_TIMEOUT_S", 0.8)
    ollama.first_delay = 60
    started = time.monotonic()
    status, text = post(web)
    body = json.loads(text)
    assert status == 504 and body["code"] == "llm_timeout" and body["detail"].startswith("Javob yozilmadi")
    assert time.monotonic() - started < 4


def test_ollama_that_is_not_running_gives_a_503(web, ollama, monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", f"http://127.0.0.1:{free_port()}")
    status, text = post(web)
    assert status == 503 and json.loads(text)["code"] == "llm_unavailable"


def test_ollama_reporting_an_error_gives_a_503_with_its_message(web, ollama):
    ollama.chat_status = 404
    status, text = post(web)
    body = json.loads(text)
    assert status == 503 and body["code"] == "llm_error" and "model 'x' not found" in body["detail"]


def test_a_model_that_goes_silent_after_some_words_ends_the_stream_with_an_error_line(web, ollama, monkeypatch):
    monkeypatch.setattr(config, "LLM_IDLE_TIMEOUT_S", 0.5)
    ollama.stall_after = 2
    status, text = post(web)
    lines = events(text)
    assert status == 200  # the status was sent with the first words; the failure is a line of the stream
    assert [e["token"] for e in lines[:-1]] == ["Lola", "-5G "]
    assert lines[-1]["code"] == "llm_timeout" and lines[-1]["error"].startswith("Javob yozilmadi")


def test_a_failure_during_retrieval_keeps_its_old_answer_and_gets_a_code_when_it_is_ollama(web, ollama, monkeypatch):
    def down(question, repo=None):
        raise ConnectionError("qdrant down")

    monkeypatch.setattr(answer, "retrieve", down)
    status, text = post(web)
    assert status == 503 and "qdrant down" in json.loads(text)["detail"] and "code" not in json.loads(text)

    from rag import llm

    def slow(question, repo=None):
        raise llm.LLMTimeout("Ollama 120 soniyada javob bermadi")

    monkeypatch.setattr(answer, "retrieve", slow)
    status, text = post(web)
    assert status == 504 and json.loads(text)["code"] == "llm_timeout"


def test_a_slow_question_does_not_block_the_server_for_other_requests(web, ollama):
    ollama.first_delay = 60
    sock = open_raw(web)
    assert ollama.chat_started.wait(5)
    conn = http.client.HTTPConnection("127.0.0.1", web, timeout=5)
    conn.request("GET", "/healthz")
    started = time.monotonic()
    assert conn.getresponse().status == 200 and time.monotonic() - started < 1
    sock.close()
