"""The bodies of the connector's routes are bounded as they arrive, not after FastAPI has read them (see
rag/connector/bodylimit.py). The middleware on its own, with scripted `receive` messages: a missing, an invalid, a
misleading and an oversized Content-Length, a chunked body, and the larger limit of the selection route."""

import asyncio

import pytest

pytest.importorskip("fastapi")

from rag.connector import bodylimit as bl  # noqa: E402

BASE = "/api/v1/connections"
SELECTIONS = f"{BASE}/11111111-1111-1111-1111-111111111111/schema/selections"
BEARER = ("authorization", "Bearer abc")


class Probe:
    """An application that reads its whole request body, as FastAPI does."""

    def __init__(self):
        self.called, self.seen = False, 0

    async def __call__(self, scope, receive, send):
        self.called = True
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            self.seen += len(message["body"])
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


class Wire:
    def __init__(self, chunks):
        self.chunks, self.requested, self.sent = list(chunks), 0, []

    async def receive(self):
        self.requested += 1
        body = self.chunks.pop(0)
        return {"type": "http.request", "body": body, "more_body": bool(self.chunks)}

    async def send(self, message):
        self.sent.append(message)

    @property
    def status(self):
        return next((m["status"] for m in self.sent if m["type"] == "http.response.start"), None)

    @property
    def body(self):
        return b"".join(m.get("body", b"") for m in self.sent if m["type"] == "http.response.body")


def drive(headers, chunks, path=BASE, method="POST"):
    probe, wire, error = Probe(), Wire(chunks), None
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
    }

    async def go():
        nonlocal error
        try:
            await asyncio.wait_for(bl.ConnectorBodyLimit(probe)(scope, wire.receive, wire.send), 3)
        except bl.BodyTooLarge as exc:
            error = exc

    asyncio.run(go())
    return probe, wire, error


KB = 1024
MB = 1024 * KB


def test_a_body_within_the_limit_is_passed_on_untouched():
    probe, wire, error = drive([("content-length", "300")], [b"x" * 100, b"x" * 200])
    assert error is None and probe.called and probe.seen == 300 and wire.status == 200


def test_a_declared_length_over_the_limit_is_refused_before_a_byte_is_read():
    probe, wire, _ = drive([("content-length", str(bl.MAX_BODY_BYTES + 1))], [b"x"])
    assert wire.status == 413 and not probe.called and wire.requested == 0
    assert wire.body == b'{"error": {"code": "request_too_large"}}'


@pytest.mark.parametrize("value", ["abc", "-1", "+5", "1e3", " ", "", "12 34", "99999999999999999999999"])
def test_an_invalid_content_length_is_a_400_and_nothing_is_read(value):
    probe, wire, _ = drive([("content-length", value)], [b"x"])
    assert wire.status == 400 and not probe.called and wire.requested == 0
    assert wire.body == b'{"error": {"code": "invalid_content_length"}}'


def test_two_different_content_lengths_are_refused():
    headers = [("content-length", "10"), ("content-length", "20")]
    assert drive(headers, [b"x"])[1].status == 400


def test_a_misleading_content_length_does_not_get_past_the_limit():
    """It says 10 bytes and sends more: the bytes that really arrive are counted."""
    chunks = [b"x" * (100 * KB)] * 3
    probe, wire, error = drive([("content-length", "10")], chunks)
    assert isinstance(error, bl.BodyTooLarge) and error.status_code == 413
    assert probe.seen <= bl.MAX_BODY_BYTES  # never more than the limit was handed on
    assert wire.requested == 3  # it stopped at the chunk that crossed the limit


def test_a_body_without_content_length_is_counted_too():
    """A chunked body has no header at all: the old check would have let any size through."""
    chunks = [b"x" * (64 * KB)] * 10
    probe, wire, error = drive([("transfer-encoding", "chunked")], chunks)
    assert isinstance(error, bl.BodyTooLarge) and probe.seen <= bl.MAX_BODY_BYTES and wire.requested < 10


def test_a_small_chunked_body_is_allowed():
    probe, wire, error = drive([("transfer-encoding", "chunked")], [b"x" * 10] * 5)
    assert error is None and probe.seen == 50 and wire.status == 200


def test_a_request_without_a_body_and_without_a_length_is_allowed():
    probe, wire, error = drive([], [b""])
    assert error is None and probe.called and wire.status == 200


def test_the_selection_route_has_the_larger_limit_only_with_a_bearer_token_and_only_for_put():
    big = str(bl.MAX_BODY_BYTES + 1)
    assert drive([BEARER, ("content-length", big)], [b"x" * 10], SELECTIONS, "PUT")[1].status == 200
    assert drive([BEARER, ("content-length", str(8 * MB + 1))], [b"x"], SELECTIONS, "PUT")[1].status == 413
    for headers, method in (([("content-length", big)], "PUT"), ([BEARER, ("content-length", big)], "POST")):
        assert drive(headers, [b"x"], SELECTIONS, method)[1].status == 413  # no token, or another method
    assert drive([("authorization", "Bearer "), ("content-length", big)], [b"x"], SELECTIONS, "PUT")[1].status == 413
    assert drive([BEARER, ("content-length", big)], [b"x"], f"{BASE}/x/descriptions", "PUT")[1].status == 413


def test_the_larger_limit_counts_real_bytes_too():
    probe, wire, error = drive([BEARER, ("content-length", "10")], [b"x" * MB] * 9, SELECTIONS, "PUT")
    assert isinstance(error, bl.BodyTooLarge) and probe.seen <= 8 * MB


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_requests_that_carry_no_body_are_not_touched(method):
    probe, wire, error = drive([("content-length", str(10 * MB))], [b"x"], BASE, method)
    assert error is None and probe.called and wire.status == 200


@pytest.mark.parametrize("path", ["/api/documents", "/api/v1/connectionsx", "/api/v1/sessions", "/"])
def test_other_paths_are_not_touched(path):
    probe, wire, error = drive([("content-length", str(10 * MB))], [b"x"], path)
    assert error is None and probe.called and wire.status == 200


def test_a_websocket_or_lifespan_scope_passes_through():
    probe, seen = Probe(), []

    async def go():
        async def app(scope, receive, send):
            seen.append(scope["type"])

        await bl.ConnectorBodyLimit(app)({"type": "lifespan"}, None, None)

    asyncio.run(go())
    assert seen == ["lifespan"] and not probe.called


# ---------- a real web server (uvicorn) and a raw socket: what the framework really does with the body ----------


@pytest.fixture
def server():
    import socket
    import threading
    import time

    import uvicorn
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    app = FastAPI()
    reads = []

    @app.put(SELECTIONS)
    @app.put(f"{BASE}/x/descriptions")
    async def read_body(request: Request):
        data = await request.body()
        reads.append(len(data))
        return JSONResponse({"read": len(data)})

    @app.exception_handler(bl.BodyTooLarge)
    async def too_large(_request, _error):
        return JSONResponse({"error": {"code": "request_too_large"}}, status_code=413)

    app.add_middleware(bl.ConnectorBodyLimit)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    instance = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not instance.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert instance.started
    yield port, reads
    instance.should_exit = True
    thread.join(10)


def raw(port, head, body_parts):
    import socket

    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(head.encode())
        try:
            for part in body_parts:
                sock.sendall(part)
        except OSError:
            pass  # the server answered and closed early: that is the point
        sock.settimeout(10)
        data = b""
        try:
            while b"\r\n\r\n" not in data or b"}" not in data:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
        except OSError:
            pass
    status = int(data.split(b" ", 2)[1]) if data else None
    return status, data


def test_a_chunked_body_with_no_length_is_cut_off_by_a_real_server(server):
    port, reads = server
    head = f"PUT {BASE}/x/descriptions HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
    parts = [b"%x\r\n%s\r\n" % (65536, b"x" * 65536)] * 20 + [b"0\r\n\r\n"]
    status, data = raw(port, head, parts)
    assert status == 413 and b"request_too_large" in data and reads == []


def test_a_declared_length_over_the_limit_is_answered_before_the_body_is_sent(server):
    port, reads = server
    head = f"PUT {BASE}/x/descriptions HTTP/1.1\r\nHost: x\r\nContent-Length: {10 * MB}\r\n\r\n"
    status, data = raw(port, head, [])  # not a byte of the body was sent, and the answer still comes
    assert status == 413 and reads == []


def test_an_invalid_length_and_a_body_within_the_limit_over_a_real_server(server):
    port, reads = server
    status, _ = raw(port, f"PUT {BASE}/x/descriptions HTTP/1.1\r\nHost: x\r\nContent-Length: 12abc\r\n\r\n", [])
    assert status == 400
    status, data = raw(port, f"PUT {BASE}/x/descriptions HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\n", [b"hello"])
    assert status == 200 and reads == [5]


def test_the_sign_in_routes_have_a_small_limit_of_their_own():
    for path in ("/api/v1/session/pairing", "/api/v1/session/exchange", "/api/v1/session/step-up"):
        too_big = str(bl.MAX_SESSION_BODY_BYTES + 1)
        assert drive([("content-length", too_big)], [b"x"], path)[1].status == 413
        assert drive([("content-length", "20")], [b'{"code": "12345678"}'], path)[1].status == 200
        probe, _wire, error = drive([("transfer-encoding", "chunked")], [b"x" * 8192] * 4, path)
        assert isinstance(error, bl.BodyTooLarge) and probe.seen <= bl.MAX_SESSION_BODY_BYTES
