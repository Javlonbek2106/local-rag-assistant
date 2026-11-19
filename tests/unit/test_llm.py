"""rag/llm.py against a fake Ollama speaking real HTTP: limits on the output, timeouts that tell slow from dead,
cancellation that really closes the connection."""
import socket
import threading
import time

import pytest

from rag import config, llm
from tests.fake_ollama import FakeOllama

MESSAGES = [{"role": "user", "content": "Savol?"}]


@pytest.fixture
def ollama(monkeypatch):
    fake = FakeOllama().start()
    monkeypatch.setenv("OLLAMA_HOST", fake.url)
    monkeypatch.setattr(config, "OLLAMA_PROBE_INTERVAL_S", 0.1)
    monkeypatch.setattr(config, "OLLAMA_UNRESPONSIVE_S", 0.5)
    monkeypatch.setattr(config, "OLLAMA_CONNECT_TIMEOUT_S", 1)
    yield fake
    fake.stop()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def collect(scope=None):
    """Everything chat_stream yields, optionally with a scope bound (as the web layer does)."""
    with llm.bind(scope or llm.Scope()):
        return list(llm.chat_stream(MESSAGES))


def in_thread(function):
    """Run `function` in a thread; returns (thread, result list). The result is the value or the exception raised."""
    result = []

    def run():
        try:
            result.append(function())
        except BaseException as error:  # noqa: BLE001
            result.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, result


# ---------- the request: output and memory limits ----------

def test_the_answer_streams_piece_by_piece(ollama):
    assert collect() == ["Lola", "-5G ", "[1]"]


def test_the_chat_request_limits_the_output_and_the_time_the_model_stays_loaded(ollama):
    collect()
    [body] = ollama.chats()
    assert body["stream"] is True and body["model"] == config.LLM_MODEL and body["messages"] == MESSAGES
    assert body["options"] == {"temperature": 0, "num_ctx": config.LLM_NUM_CTX, "num_predict": config.LLM_NUM_PREDICT}
    assert body["keep_alive"] == config.OLLAMA_KEEP_ALIVE
    assert 0 < config.LLM_NUM_PREDICT <= 1024  # the output is bounded by default


def test_the_limits_follow_the_configuration(ollama, monkeypatch):
    monkeypatch.setattr(config, "LLM_NUM_PREDICT", 77)
    monkeypatch.setattr(config, "OLLAMA_KEEP_ALIVE", "-1")
    collect()
    assert ollama.chats()[0]["options"]["num_predict"] == 77 and ollama.chats()[0]["keep_alive"] == "-1"


def test_an_answer_cut_at_the_output_limit_is_marked_truncated(ollama):
    ollama.done_reason = "length"
    scope = llm.Scope()
    collect(scope)
    assert scope.truncated is True


def test_an_answer_that_finished_is_not_marked_truncated(ollama):
    scope = llm.Scope()
    collect(scope)
    assert scope.truncated is False


def test_embeddings_are_requested_in_batches_and_keep_the_model_loaded(ollama, monkeypatch):
    monkeypatch.setattr(config, "EMBED_BATCH", 2)
    vectors = llm.embed(["a", "b", "c", "d", "e"])
    assert len(vectors) == 5 and vectors[0] == [0.1, 0.2, 0.3]
    bodies = [b for path, b in ollama.requests if path == "/api/embed"]
    assert [len(b["input"]) for b in bodies] == [2, 2, 1]
    assert all(b["model"] == config.EMBED_MODEL and b["keep_alive"] == config.OLLAMA_KEEP_ALIVE for b in bodies)


def test_warm_up_loads_both_models_and_never_raises(ollama, monkeypatch):
    llm.warm_up()
    paths = [path for path, _ in ollama.requests]
    assert paths == ["/api/embed", "/api/generate"]
    assert ollama.requests[1][1] == {"model": config.LLM_MODEL, "keep_alive": config.OLLAMA_KEEP_ALIVE, "stream": False}
    monkeypatch.setenv("OLLAMA_HOST", f"http://127.0.0.1:{free_port()}")
    llm.warm_up()  # Ollama is not running: only a log line


# ---------- Ollama is not answering: a clear error, soon ----------

def test_ollama_that_is_not_running_is_reported_without_waiting_for_a_timeout(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", f"http://127.0.0.1:{free_port()}")
    started = time.monotonic()
    with pytest.raises(llm.LLMUnavailable, match="Ollama"):
        llm.embed(["x"])
    with pytest.raises(llm.LLMUnavailable):
        collect()
    assert time.monotonic() - started < 8  # Windows takes about 2 s to refuse a connection; no timeout is waited for


def test_ollama_that_accepts_the_connection_but_never_answers_is_reported_unavailable(ollama):
    ollama.hang_everything = True
    started = time.monotonic()
    with pytest.raises(llm.LLMUnavailable, match="javob bermayapti"):
        collect()
    elapsed = time.monotonic() - started
    slack = 3 * config.OLLAMA_PROBE_INTERVAL_S + 1
    assert config.OLLAMA_UNRESPONSIVE_S <= elapsed < config.OLLAMA_UNRESPONSIVE_S + slack
    with pytest.raises(llm.LLMUnavailable):
        llm.embed(["x"])


def test_with_the_default_settings_a_silent_ollama_is_reported_within_30_seconds():
    # the bound: no sign of life for OLLAMA_UNRESPONSIVE_S, noticed on the next probe (one probe interval, and the
    # probe itself is cut after at most one more), so it is measured in the live test and bounded here by the settings
    worst = config.OLLAMA_UNRESPONSIVE_S + 2 * config.OLLAMA_PROBE_INTERVAL_S
    assert worst <= 30


def test_a_slow_but_living_ollama_is_waited_for_beyond_a_plain_read_timeout(ollama, monkeypatch):
    # The first token (and with it the HTTP response) takes 1.2 s: longer than the 0.5 s Ollama may stay silent. It
    # answers the "are you alive?" question meanwhile, so the request is allowed to take its time.
    monkeypatch.setattr(config, "LLM_FIRST_TOKEN_TIMEOUT_S", 10)
    ollama.first_delay = 1.2
    started = time.monotonic()
    assert collect() == ["Lola", "-5G ", "[1]"]
    assert time.monotonic() - started >= 1.2


def test_a_living_ollama_that_is_too_slow_is_a_timeout_not_unavailable(ollama, monkeypatch):
    monkeypatch.setattr(config, "LLM_FIRST_TOKEN_TIMEOUT_S", 0.8)
    ollama.first_delay = 30
    started = time.monotonic()
    with pytest.raises(llm.LLMTimeout, match="javob bermadi"):
        collect()
    assert 0.8 <= time.monotonic() - started < 3


def test_a_slow_embedding_is_a_timeout(ollama, monkeypatch):
    monkeypatch.setattr(config, "EMBED_TIMEOUT_S", 0.6)
    ollama.embed_delay = 30
    with pytest.raises(llm.LLMTimeout):
        llm.embed(["x"])


def test_a_model_that_goes_silent_in_the_middle_of_an_answer_is_a_timeout(ollama, monkeypatch):
    monkeypatch.setattr(config, "LLM_IDLE_TIMEOUT_S", 0.5)
    ollama.stall_after = 2
    seen = []
    started = time.monotonic()
    with llm.bind(llm.Scope()):
        with pytest.raises(llm.LLMTimeout):
            for piece in llm.chat_stream(MESSAGES):
                seen.append(piece)
    assert seen == ["Lola", "-5G "] and time.monotonic() - started < 3  # what was written is not lost


def test_an_answer_that_never_ends_is_cut_by_the_total_limit(ollama, monkeypatch):
    monkeypatch.setattr(config, "LLM_TOTAL_TIMEOUT_S", 0.6)
    ollama.endless, ollama.token_delay = True, 0.05
    started = time.monotonic()
    with pytest.raises(llm.LLMTimeout, match="tugamadi"):
        collect()
    assert 0.6 <= time.monotonic() - started < 3
    assert ollama.closed.wait(1)  # and the connection was closed, so Ollama stopped too


def test_an_error_from_ollama_is_reported_with_its_message(ollama):
    ollama.chat_status = 404
    with pytest.raises(llm.LLMError, match="model 'x' not found") as info:
        collect()
    assert not isinstance(info.value, (llm.LLMTimeout, llm.LLMUnavailable))
    ollama.chat_status = 200
    ollama.embed_status = 404
    with pytest.raises(llm.LLMError, match="HTTP 404"):
        llm.embed(["x"])


def test_an_error_line_in_the_middle_of_the_stream_is_reported(ollama):
    ollama.error_after = 1
    with pytest.raises(llm.LLMError, match="llama runner crashed"):
        collect()


# ---------- cancellation ----------

def test_cancelling_a_running_answer_stops_it_and_closes_the_connection_to_ollama_at_once(ollama):
    ollama.endless, ollama.token_delay = True, 0.05
    scope, got_first = llm.Scope(), threading.Event()

    def consume():
        with llm.bind(scope):
            for _piece in llm.chat_stream(MESSAGES):
                got_first.set()

    thread, result = in_thread(consume)
    assert got_first.wait(2)
    before = time.monotonic()
    scope.cancel()
    thread.join(2)
    assert not thread.is_alive() and isinstance(result[0], llm.LLMCancelled)
    assert ollama.closed.wait(1)
    assert ollama.closed_at - before < 0.5  # Ollama saw the connection close almost immediately
    assert scope._sockets == []  # nothing is left registered


def test_cancelling_while_waiting_for_the_first_token_stops_the_wait_at_once(ollama, monkeypatch):
    monkeypatch.setattr(config, "LLM_FIRST_TOKEN_TIMEOUT_S", 30)
    ollama.first_delay = 30  # the model is still reading the prompt
    scope = llm.Scope()
    thread, result = in_thread(lambda: collect(scope))
    assert ollama.chat_started.wait(2)
    time.sleep(0.2)
    before = time.monotonic()
    scope.cancel()
    thread.join(2)
    assert not thread.is_alive() and isinstance(result[0], llm.LLMCancelled)
    assert time.monotonic() - before < 0.5
    assert ollama.closed.wait(1) and ollama.closed_at - before < 0.5


def test_cancelling_while_ollama_is_being_asked_if_it_is_alive_is_not_delayed_by_the_question(ollama, monkeypatch):
    monkeypatch.setattr(config, "OLLAMA_PROBE_INTERVAL_S", 0.1)
    monkeypatch.setattr(config, "OLLAMA_UNRESPONSIVE_S", 30)
    ollama.hang_everything = True  # the request hangs, and so does every probe
    scope = llm.Scope()
    thread, result = in_thread(lambda: collect(scope))
    assert ollama.closed.wait(0) is False
    time.sleep(0.5)  # several probes are in flight by now
    before = time.monotonic()
    scope.cancel()
    thread.join(2)
    assert not thread.is_alive() and isinstance(result[0], llm.LLMCancelled)
    assert time.monotonic() - before < 0.5


def test_a_scope_cancelled_before_the_call_sends_nothing(ollama):
    scope = llm.Scope()
    scope.cancel()
    with pytest.raises(llm.LLMCancelled):
        collect(scope)
    with llm.bind(scope):
        with pytest.raises(llm.LLMCancelled):
            llm.embed(["x"])
    assert ollama.requests == []


def test_a_consumer_that_stops_reading_closes_the_connection(ollama):
    ollama.endless, ollama.token_delay = True, 0.05
    stream = llm.chat_stream(MESSAGES)
    assert next(stream) == "Lola"
    before = time.monotonic()
    stream.close()  # what happens to an abandoned generator
    assert ollama.closed.wait(1) and ollama.closed_at - before < 0.5


def test_cancelling_one_scope_leaves_the_others_running(ollama):
    ollama.serial = False
    ollama.endless, ollama.token_delay = True, 0.02
    first, second = llm.Scope(), llm.Scope()
    counts = {"first": 0, "second": 0}

    def consume(name, scope):
        def run():
            with llm.bind(scope):
                for _piece in llm.chat_stream(MESSAGES):
                    counts[name] += 1
        return run

    a, _ = in_thread(consume("first", first))
    b, result_b = in_thread(consume("second", second))
    time.sleep(0.3)
    first.cancel()
    a.join(2)
    seen = counts["second"]
    time.sleep(0.3)
    assert counts["second"] > seen and b.is_alive()  # the other request went on
    second.cancel()
    b.join(2)
    assert isinstance(result_b[0], llm.LLMCancelled)


def test_the_scope_of_one_thread_is_not_seen_by_another(ollama):
    scope = llm.Scope()
    scope.cancel()
    seen = []
    with llm.bind(scope):
        thread = threading.Thread(target=lambda: seen.append(list(llm.chat_stream(MESSAGES))))
        thread.start()
        thread.join(2)
    assert seen == [["Lola", "-5G ", "[1]"]]  # the other thread was not bound to the cancelled scope
