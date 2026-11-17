"""Roadmap T0.5: an upload body larger than the limit is refused early, before it is written to the disk.

Three levels: the middleware on its own (a bare ASGI call), the whole application through the test client, and a real
web server (uvicorn) with a raw-socket client, which is how the audit measured the problem (API-02: 150 MB sent against
a 1 MB limit left 139 MB in the temporary folder). Disk use is measured two ways: the size of the temporary folder, and
the bytes that Starlette's multipart parser writes into its temporary files (the second works on every operating
system, because on Linux an open temporary file has no name in the folder).
"""
import asyncio
import json
import select
import socket
import tempfile
import threading
import time

import pytest
import starlette.formparsers
import uvicorn
from fastapi import HTTPException

from rag import answer, config
from rag.api import create_app
from rag.bodylimit import UploadBodyLimit, declared_length, request_limit
from tests.helpers import make_hit

MiB = 1024 * 1024


# ---------- the middleware on its own ----------

class Probe:
    """An application that reads its whole request body."""

    def __init__(self):
        self.called, self.seen, self.steps = False, 0, 0

    async def __call__(self, scope, receive, send):
        self.called = True
        while True:
            message = await receive()
            self.steps += 1
            if message["type"] != "http.request":
                break
            self.seen += len(message["body"])
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


class Wire:
    """What a server would hand to the application: scripted `receive` messages, recorded `send` messages."""

    def __init__(self, chunks):
        self.chunks, self.requested, self.sent = list(chunks), 0, []

    async def receive(self):
        self.requested += 1
        if not self.chunks:
            await asyncio.Event().wait()  # nothing more is coming (a client that went quiet)
        body = self.chunks.pop(0)
        return {"type": "http.request", "body": body, "more_body": bool(self.chunks)}

    async def send(self, message):
        self.sent.append(message)

    @property
    def status(self):
        return next((m["status"] for m in self.sent if m["type"] == "http.response.start"), None)


def scope(headers=(), path="/api/documents", method="POST"):
    return {"type": "http", "method": method, "path": path,
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers]}


def drive(headers, chunks, path="/api/documents", method="POST"):
    probe, wire = Probe(), Wire(chunks)
    error = None

    async def go():
        nonlocal error
        try:
            await asyncio.wait_for(UploadBodyLimit(probe)(scope(headers, path, method), wire.receive, wire.send), 3)
        except HTTPException as exc:
            error = exc

    asyncio.run(go())
    return probe, wire, error


@pytest.fixture
def small_limit(monkeypatch):
    """A limit of 1000 bytes for a file and 500 for the multipart framing: a request may carry 1500."""
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 1000)
    monkeypatch.setattr(config, "UPLOAD_FRAMING_BYTES", 500)
    return 1500


def test_the_limit_of_a_request_is_the_file_limit_plus_the_framing(small_limit):
    assert request_limit() == small_limit


def test_a_request_below_the_limit_is_passed_on_untouched(small_limit):
    probe, wire, error = drive([("content-length", "900")], [b"x" * 400, b"x" * 500])
    assert error is None and probe.called and probe.seen == 900 and wire.status == 200


@pytest.mark.parametrize("length", [1000, 1500])
def test_a_request_exactly_as_large_as_the_file_limit_or_the_whole_limit_is_accepted(small_limit, length):
    probe, wire, error = drive([("content-length", str(length))], [b"x" * length])
    assert error is None and probe.seen == length and wire.status == 200


def test_a_declared_length_over_the_limit_is_answered_at_once_and_the_body_is_never_read(small_limit):
    probe, wire, error = drive([("content-length", "1501")], [b"x" * 1501])
    assert error is None and wire.status == 413
    assert not probe.called and wire.requested == 0  # the application was not even started; no byte was asked for
    reply = json.loads(next(m["body"] for m in wire.sent if m["type"] == "http.response.body"))
    assert "detail" in reply and "MB" in reply["detail"]
    headers = dict(next(m["headers"] for m in wire.sent if m["type"] == "http.response.start"))
    assert headers[b"content-type"] == b"application/json" and b"connection" not in headers



def test_a_body_longer_than_it_announced_is_counted_and_stopped_at_the_limit(small_limit):
    chunks = [b"x" * 400] * 10  # 4000 bytes in pieces although 1500 were announced
    probe, wire, error = drive([("content-length", "1500")], chunks)
    assert isinstance(error, HTTPException) and error.status_code == 413
    assert probe.seen == 1200 and wire.requested == 4  # stopped at the chunk that crossed 1500; 6 chunks never read



def test_without_a_length_the_request_is_refused_with_411_and_not_a_byte_is_read(small_limit):
    probe, wire, error = drive([("transfer-encoding", "chunked")], [b"x" * 400] * 3)  # a small body nobody announced
    assert error is None and wire.status == 411
    assert not probe.called and wire.requested == 0  # the application was not started, no byte was asked for
    reply = json.loads(next(m["body"] for m in wire.sent if m["type"] == "http.response.body"))
    assert set(reply) == {"detail"} and "Content-Length" in reply["detail"]
    headers = dict(next(m["headers"] for m in wire.sent if m["type"] == "http.response.start"))
    assert headers[b"content-type"] == b"application/json"


def test_no_length_at_all_is_a_411_also_for_a_request_that_sends_nothing(small_limit):
    probe, wire, error = drive([], [])
    assert wire.status == 411 and not probe.called


def test_a_body_longer_than_its_header_claims_is_judged_by_what_really_arrives(small_limit):
    """Some server may hand over more than Content-Length announced: the header is not trusted."""
    probe, wire, error = drive([("content-length", "100")], [b"x" * 400] * 10)
    assert isinstance(error, HTTPException) and error.status_code == 413 and probe.seen == 1200


@pytest.mark.parametrize("value", ["abc", "-5", "1e3", "1 2", "0x10", "", "+5", "1.5", "9" * 40, "１２３"])
def test_a_malformed_length_is_refused_and_cannot_be_used_to_slip_past(small_limit, value):
    probe, wire, error = drive([("content-length", value)], [b"x" * 10])
    assert wire.status == 400 and not probe.called and wire.requested == 0


def test_two_different_lengths_are_refused(small_limit):
    probe, wire, error = drive([("content-length", "10"), ("content-length", "5000")], [b"x" * 10])
    assert wire.status == 400 and not probe.called


def test_the_same_length_twice_is_just_that_length(small_limit):
    assert declared_length([(b"content-length", b"10"), (b"content-length", b" 10 ")]) == 10


@pytest.mark.parametrize("path,method", [
    ("/api/ask", "POST"), ("/api/documents", "GET"), ("/api/documents/a.txt", "DELETE"),
    ("/api/documents/a.txt/rollback", "POST"), ("/healthz", "GET")])
def test_no_other_endpoint_is_limited(small_limit, path, method):
    probe, wire, error = drive([("content-length", "50000")], [b"x" * 50000], path=path, method=method)
    assert error is None and probe.called and probe.seen == 50000 and wire.status == 200



def test_every_request_is_judged_on_its_own_even_when_they_run_at_the_same_time(small_limit):
    async def all_at_once():
        results = {}

        async def one(name, headers, chunks):
            probe, wire = Probe(), Wire(chunks)
            try:
                await asyncio.wait_for(UploadBodyLimit(probe)(scope(headers), wire.receive, wire.send), 3)
                results[name] = wire.status
            except HTTPException as exc:
                results[name] = exc.status_code

        await asyncio.gather(
            one("longer-than-announced", [("content-length", "1000")], [b"x" * 400] * 10),
            one("small-1", [("content-length", "1200")], [b"x" * 400] * 3),
            one("longer-2", [("content-length", "1000")], [b"x" * 800] * 5),
            one("small-2", [("content-length", "100")], [b"x" * 100]),
            one("no-length", [("transfer-encoding", "chunked")], [b"x" * 100]))
        return results

    assert asyncio.run(all_at_once()) == {"longer-than-announced": 413, "small-1": 200, "longer-2": 413, "small-2": 200,
                                          "no-length": 411}


# ---------- the whole application ----------

class SpoolMeter:
    """Counts what the multipart parser writes into its temporary files, and how many it opens and closes. Files are
    opened on the event loop and closed on worker threads, after the response has gone out: the counts are changed under
    a lock, and a test that looks at them right after an answer can wait for the closing (wait_until_all_closed)."""

    def __init__(self):
        self.written = self.opened = self.closed = 0
        self.changed = threading.Condition()

    def file_opened(self):
        with self.changed:
            self.opened += 1
            self.changed.notify_all()

    def file_closed(self):
        with self.changed:
            self.closed += 1
            self.changed.notify_all()

    def wait_until_all_closed(self, seconds=10):
        """Returns when every file that was opened is closed, or after `seconds`: a file that is never closed is still
        found by the caller's assertion. It waits for that event, it does not sleep."""
        with self.changed:
            self.changed.wait_for(lambda: self.opened == self.closed, seconds)


@pytest.fixture
def meter(monkeypatch, tmp_path):
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    meter = SpoolMeter()
    meter.dir = tmp

    class Counted(tempfile.SpooledTemporaryFile):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            meter.file_opened()

        def write(self, data):
            meter.written += len(data)
            return super().write(data)

        def close(self):
            was_open = not self.closed
            super().close()
            if was_open:  # counted once the file really is closed, so a waiter wakes up when it is
                meter.file_closed()

    monkeypatch.setattr(starlette.formparsers, "SpooledTemporaryFile", Counted)
    return meter


def folder_size(path):
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def multipart(name, size, boundary="BOUND"):
    """(head, filler, tail) of a multipart body with one file of `size` bytes; filler is the repeated block."""
    head = (f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n").encode()
    return head, size, f"\r\n--{boundary}--\r\n".encode()


def multipart_bytes(name, content, boundary="BOUND"):
    head, _, tail = multipart(name, len(content), boundary)
    return head + content + tail


MULTIPART = {"content-type": "multipart/form-data; boundary=BOUND"}


def test_a_normal_upload_still_works(client, repo, storage_dir, meter):
    response = client.post("/api/documents", files=[("files", ("a.txt", b"matn"))])
    assert response.status_code == 200 and response.json()["results"][0]["accepted"] is True
    assert repo.docs["a.txt"]["state"] == "queued"


def test_several_files_in_one_request_still_work(client, repo, storage_dir):
    response = client.post("/api/documents", files=[("files", ("a.txt", b"bir")), ("files", ("b.txt", b"ikki"))])
    assert [r["accepted"] for r in response.json()["results"]] == [True, True] and set(repo.docs) == {"a.txt", "b.txt"}


def test_a_request_over_the_limit_gets_413_in_the_apis_own_error_format_and_nothing_is_written(
        client, repo, storage_dir, meter, small_limit):
    response = client.post("/api/documents", files=[("files", ("a.txt", b"x" * 5000))])
    assert response.status_code == 413 and set(response.json()) == {"detail"}  # the same shape as every other API error
    assert "MB" in response.json()["detail"]
    assert meter.opened == 0 and meter.written == 0 and folder_size(meter.dir) == 0  # the body was never read
    assert repo.docs == {} and list((storage_dir / ".uploading").iterdir()) == []


def test_a_chunked_upload_through_the_whole_application_gets_411_in_the_apis_error_format(client, repo, storage_dir,
                                                                                         meter, small_limit):
    before = sorted(p.relative_to(storage_dir).as_posix() for p in storage_dir.rglob("*"))

    def pieces():
        yield b"--BOUND\r\n"
        yield b"x" * 100

    response = client.post("/api/documents", content=pieces(), headers=MULTIPART)  # no Content-Length: chunked
    assert response.status_code == 411 and set(response.json()) == {"detail"}
    assert "Content-Length" in response.json()["detail"]
    assert meter.opened == 0 and meter.written == 0 and folder_size(meter.dir) == 0
    assert sorted(p.relative_to(storage_dir).as_posix() for p in storage_dir.rglob("*")) == before and repo.docs == {}
    assert "default-src 'self'" in response.headers["content-security-policy"]
    foreign = client.post("/api/documents", content=pieces(), headers={**MULTIPART, "origin": "http://evil.com"})
    assert foreign.status_code == 403  # the guard still answers first


def test_the_security_headers_and_checks_still_apply_to_the_refusal(client, small_limit):
    response = client.post("/api/documents", files=[("files", ("a.txt", b"x" * 5000))])
    assert response.status_code == 413 and "default-src 'self'" in response.headers["content-security-policy"]
    assert client.post("/api/documents", files=[("files", ("a.txt", b"x" * 5000))],
                       headers={"origin": "http://evil.com"}).status_code == 403  # the guard answers first
    assert client.post("/api/documents", files=[("files", ("a.txt", b"x" * 5000))],
                       headers={"host": "evil.com"}).status_code == 403


def test_a_file_of_exactly_the_allowed_size_is_accepted_and_one_byte_more_gets_the_per_file_message(
        client, repo, storage_dir, small_limit):
    ok = client.post("/api/documents", files=[("files", ("a.txt", b"x" * 1000))])
    assert ok.status_code == 200 and ok.json()["results"][0]["accepted"] is True
    too_big = client.post("/api/documents", files=[("files", ("b.txt", b"x" * 1001))])
    assert too_big.status_code == 200  # the whole request fits the limit: the message of the handler applies, as before
    assert too_big.json()["results"][0]["accepted"] is False and "MB" in too_big.json()["results"][0]["error"]


def test_a_request_exactly_as_large_as_the_limit_is_accepted_and_one_byte_more_is_refused(client, small_limit):
    head, _, tail = multipart("c.txt", 0)
    room = small_limit - len(head) - len(tail)
    exact = client.post("/api/documents", content=multipart_bytes("c.txt", b"x" * room), headers=MULTIPART)
    assert exact.status_code == 200 and len(head) + room + len(tail) == small_limit
    over = client.post("/api/documents", content=multipart_bytes("c.txt", b"x" * (room + 1)), headers=MULTIPART)
    assert over.status_code == 413


@pytest.mark.parametrize("path,method", [
    ("/api/ask", "POST"), ("/api/documents", "GET"), ("/api/documents/a.txt", "DELETE"),
    ("/api/documents/a.txt/rollback", "POST"), ("/healthz", "GET")])
def test_no_other_endpoint_needs_a_length(small_limit, path, method):
    probe, wire, error = drive([("transfer-encoding", "chunked")], [b"x" * 50000], path=path, method=method)
    assert error is None and probe.called and probe.seen == 50000 and wire.status == 200


def test_the_other_endpoints_are_not_affected(client, repo, monkeypatch, small_limit):
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 10)
    monkeypatch.setattr(config, "UPLOAD_FRAMING_BYTES", 10)
    monkeypatch.setattr(answer, "retrieve", lambda q, repo=None: [make_hit()])
    monkeypatch.setattr(answer, "stream_answer", lambda q, h: iter(["Javob [1]"]))
    assert client.get("/api/documents").status_code == 200 and client.get("/healthz").status_code == 200
    asked = client.post("/api/ask", json={"question": "s" * 500})  # far more than the 20 bytes an upload could carry
    assert asked.status_code == 200 and '"final"' in asked.text
    assert client.post("/api/documents/a.txt/rollback", json={"version": 1}).status_code in (404, 409)
    assert client.delete("/api/documents/a.txt").status_code == 200


# ---------- a real server and a raw-socket client ----------

@pytest.fixture
def web(storage_dir, repo, qdrant, monkeypatch, meter):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
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


class Result:
    def __init__(self, status, body, sent, seconds, reset=False):
        self.status, self.body, self.sent, self.seconds, self.reset = status, body, sent, seconds, reset

    def __repr__(self):
        return f"Result(status={self.status}, sent={self.sent}, in {self.seconds:.2f}s, reset={self.reset})"


def read_response(sock, wait=5):
    sock.settimeout(0.2)
    data, end = b"", time.monotonic() + wait
    while time.monotonic() < end:
        try:
            part = sock.recv(65536)
        except TimeoutError:
            continue
        except OSError:
            break
        if not part:
            break
        data += part
        head, _, rest = data.partition(b"\r\n\r\n")
        if rest or b"\r\n\r\n" in data:
            lines = head.split(b"\r\n")
            length = next((int(x.split(b":")[1]) for x in lines if x.lower().startswith(b"content-length")), 0)
            if len(rest) >= length:
                break
    status = int(data.split(b" ")[1]) if data.startswith(b"HTTP/") else None
    return status, data.partition(b"\r\n\r\n")[2]


def how_the_connection_ends(sock, wait=5):
    """What the server does with a connection after its last answer: (what still arrived, "eof" | "reset" | "open").
    "open" means it neither closed nor reset it within `wait` seconds."""
    sock.settimeout(0.2)
    data, end = b"", time.monotonic() + wait
    while time.monotonic() < end:
        try:
            part = sock.recv(65536)
        except TimeoutError:
            continue
        except OSError:
            return data, "reset"
        if not part:
            return data, "eof"
        data += part
    return data, "open"


def post(port, headers, body, chunked=False, pause=0.0, limit_bytes=None, expect=False, keep_sending=False,
         answer_first=False):
    """Send a request the way a well-behaved client does: stop as soon as the server has answered. `body` is an
    iterable of byte blocks (never held in memory as a whole). Returns what was answered and how much was sent.

    `answer_first`: for a request whose head the server refuses on its own, before there is any body to speak of (a
    Content-Length that is not a number). The server then answers and closes the connection; bytes that the client
    sends after that close make the operating system reset the connection, and a reset can destroy the answer that
    is already on its way (measured: ConnectionResetError instead of the 400, a few times in 300 on a busy machine).
    Such a client waits for the answer; the body is sent only if the server has not answered within 5 seconds."""
    started = time.monotonic()
    sock = socket.create_connection(("127.0.0.1", port))
    head = "POST /api/documents HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n" % port
    head += "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    if chunked:
        head += "Transfer-Encoding: chunked\r\n"
    if expect:
        head += "Expect: 100-continue\r\n"
    sock.sendall(head.encode() + b"\r\n")
    sent, reset = 0, False
    if expect or answer_first:  # a client that waits for the go-ahead (or an answer) before it sends the body
        ready, _, _ = select.select([sock], [], [], 5.0 if answer_first else 1.0)
        if ready:
            status, payload = read_response(sock, 2)
            sock.close()
            return Result(status, payload, 0, time.monotonic() - started)
    for block in body:
        ready, _, _ = select.select([sock], [], [], 0)
        if ready and not keep_sending:
            break  # the server has said something (or hung up): stop sending
        try:
            sock.sendall((b"%x\r\n" % len(block) + block + b"\r\n") if chunked else block)
        except OSError:
            reset = True
            break
        sent += len(block)
        if limit_bytes is not None and sent >= limit_bytes:
            break
        if pause:
            time.sleep(pause)
    else:
        if chunked:
            try:
                sock.sendall(b"0\r\n\r\n")
            except OSError:
                reset = True
    status, payload = read_response(sock)
    sock.close()
    return Result(status, payload, sent, time.monotonic() - started, reset)


def blocks(size, block=64 * 1024):
    left = size
    while left > 0:
        n = min(block, left)
        yield b"a" * n
        left -= n


def multipart_blocks(name, size):
    head, _, tail = multipart(name, size)
    yield head
    yield from blocks(size)
    yield tail


def multipart_length(name, size):
    head, _, tail = multipart(name, size)
    return len(head) + size + len(tail)


@pytest.fixture
def audit_limit(monkeypatch):
    """The audit's scenario: a 1 MB limit."""
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 1 * MiB)


def test_the_audit_scenario_150_mb_against_a_1_mb_limit_is_refused_before_the_body_is_written(web, repo, meter,
                                                                                              audit_limit):
    declared = multipart_length("katta.pdf", 150 * MiB)
    before = folder_size(meter.dir)
    result = post(web, {**MULTIPART, "Content-Length": declared}, multipart_blocks("katta.pdf", 150 * MiB))
    after = folder_size(meter.dir)
    print(f"\n150 MB with Content-Length {declared}: {result}; temporary folder {before} -> {after} bytes; "
          f"multipart parser wrote {meter.written} bytes")
    assert result.status == 413 and b"detail" in result.body
    assert result.sent < 5 * MiB  # a client that watches for the answer stops almost at once (the rest never leaves)
    assert meter.written == 0 and meter.opened == 0  # not a single byte reached a temporary file
    assert after - before < 5 * MiB and after == 0
    assert result.seconds < 5 and repo.docs == {}


def test_a_client_that_keeps_sending_like_a_browser_still_receives_the_413(web, repo, meter, audit_limit):
    """Closing the connection instead would reset it and the browser would never see the answer (measured)."""
    declared = multipart_length("katta.pdf", 60 * MiB)
    result = post(web, {**MULTIPART, "Content-Length": declared}, multipart_blocks("katta.pdf", 60 * MiB),
                  keep_sending=True)
    assert result.status == 413 and b"detail" in result.body and not result.reset
    assert result.sent == declared  # it sent everything, the server read it and threw it away...
    assert meter.written == 0 and meter.opened == 0 and folder_size(meter.dir) == 0  # ...without storing any of it


def test_the_same_with_expect_100_continue_the_body_is_never_sent(web, repo, meter, audit_limit):
    declared = multipart_length("katta.pdf", 150 * MiB)
    result = post(web, {**MULTIPART, "Content-Length": declared}, multipart_blocks("katta.pdf", 150 * MiB), expect=True)
    assert result.status == 413 and result.sent == 0 and meter.written == 0



def test_a_chunked_upload_without_a_length_is_refused_with_411_before_anything_is_written(web, repo, meter, storage_dir,
                                                                                         audit_limit):
    storage_before = sorted(p.relative_to(storage_dir).as_posix() for p in storage_dir.rglob("*"))
    before = folder_size(meter.dir)
    result = post(web, MULTIPART, multipart_blocks("katta.pdf", 150 * MiB), chunked=True)
    after = folder_size(meter.dir)
    print(f"\n150 MB chunked, no Content-Length: {result}; temporary folder {before} -> {after} bytes; "
          f"multipart parser wrote {meter.written} bytes")
    assert result.status == 411 and b"Content-Length" in result.body
    assert result.sent < 5 * MiB  # a client that watches for the answer stops almost at once
    assert meter.written == 0 and meter.opened == 0 and after == before == 0  # TEMP did not move
    storage_after = sorted(p.relative_to(storage_dir).as_posix() for p in storage_dir.rglob("*"))
    assert storage_after == storage_before  # nor storage
    assert repo.docs == {}


def test_a_client_that_keeps_sending_a_chunked_body_still_receives_the_411(web, repo, meter, audit_limit):
    result = post(web, MULTIPART, multipart_blocks("katta.pdf", 60 * MiB), chunked=True, keep_sending=True)
    assert result.status == 411 and b"detail" in result.body and not result.reset
    assert meter.written == 0 and meter.opened == 0 and folder_size(meter.dir) == 0


def test_with_expect_100_continue_a_chunked_body_is_refused_and_never_sent(web, repo, meter, audit_limit):
    result = post(web, MULTIPART, multipart_blocks("katta.pdf", 150 * MiB), chunked=True, expect=True)
    assert result.status == 411 and result.sent == 0 and meter.written == 0


def test_the_other_endpoints_do_not_need_a_length_at_a_real_server(web, repo, meter, monkeypatch):
    import http.client

    monkeypatch.setattr(answer, "retrieve", lambda q, repo=None: [make_hit()])
    monkeypatch.setattr(answer, "stream_answer", lambda q, h: iter(["Javob [1]"]))
    conn = http.client.HTTPConnection("127.0.0.1", web, timeout=10)
    conn.request("POST", "/api/ask", body=iter([b'{"question":', b' "savol"}']), encode_chunked=True,
                 headers={"Content-Type": "application/json"})  # a chunked JSON body, no Content-Length
    response = conn.getresponse()
    assert response.status == 200 and b'"final"' in response.read()
    conn.request("GET", "/api/documents")
    assert conn.getresponse().status == 200
    conn.close()



def test_even_a_tiny_chunked_upload_without_a_length_is_refused(web, repo, meter, audit_limit):
    result = post(web, MULTIPART, multipart_blocks("a.txt", 4000), chunked=True)
    assert result.status == 411 and repo.docs == {} and meter.written == 0


def test_a_small_upload_with_a_length_is_accepted(web, repo, meter, audit_limit):
    headers = {**MULTIPART, "Content-Length": multipart_length("b.txt", 5000)}
    result = post(web, headers, multipart_blocks("b.txt", 5000))
    assert result.status == 200 and "b.txt" in repo.docs


def test_a_length_that_is_not_a_number_is_refused(web, repo, meter, audit_limit):
    result = post(web, {**MULTIPART, "Content-Length": "abc"}, [b"x" * 10], answer_first=True)
    assert result.status == 400 and result.sent == 0  # refused on the head alone: no body byte was needed or sent
    assert repo.docs == {} and meter.written == 0


def test_a_header_that_claims_less_than_what_is_sent_cannot_get_the_surplus_through(web, repo, meter, audit_limit):
    """The server itself stops reading at the announced length (100 bytes); the rest is not a request. What the
    application sees is an incomplete multipart body, which it refuses: 422, no file in it (always: measured 600 times
    on Windows, idle and loaded). The 413 for a body that really is longer than its header is proved at the middleware
    level above, with a server that hands over the surplus.

    The order matters and is what makes the test exact. The client sends the 100 announced bytes and READS the answer
    to that request. Only then does it send the surplus (5 MB). That is not a continuation of the first request and not
    a valid request of its own, so the server ends the connection: a reset (Windows) or a 400 and a close (Linux). A
    client that sent the surplus at once would lose the 422 to that reset on Windows (status None in 22-36 % of 600
    measured attempts): which is why `None` is not an accepted answer here."""
    stream = multipart_blocks("liar.pdf", 5 * MiB)
    first = next(stream)
    sock = socket.create_connection(("127.0.0.1", web))
    head = (f"POST /api/documents HTTP/1.1\r\nHost: 127.0.0.1:{web}\r\ncontent-type: {MULTIPART['content-type']}\r\n"
            "Content-Length: 100\r\n\r\n")
    sock.sendall(head.encode() + first[:100])  # exactly the announced 100 bytes of the body
    status, payload = read_response(sock)
    assert status == 422 and b"detail" in payload  # the answer to the request the header described
    try:
        sock.sendall(first[100:])
        for block in stream:
            sock.sendall(block)
    except OSError:
        pass  # the server has already ended the connection: that is the expected reaction to the surplus
    after, ended = how_the_connection_ends(sock)
    sock.close()
    print(f"\nContent-Length 100, 5 MB sent: first answer {status}; then {after[:40]!r}, connection {ended}; "
          f"multipart parser wrote {meter.written} bytes")
    assert ended in ("eof", "reset") and not after.startswith(b"HTTP/1.1 2")  # ended, and never accepted as a request
    assert meter.written <= 100 and repo.docs == {}



def test_a_multipart_body_stopped_in_the_middle_leaves_no_temporary_file_behind(repo, meter, storage_dir, small_limit,
                                                                               monkeypatch):
    """The path of a body that is longer than it announced, through the whole application and the real multipart
    parser (a real server cannot hand over such a surplus): the parser has already opened files when the 413 comes."""
    monkeypatch.setattr(starlette.formparsers.MultiPartParser, "spool_max_size", 100)  # files go to the disk at once
    app = create_app(repo)
    body = multipart_bytes("katta.txt", b"x" * 6000)
    chunks = [body[i:i + 400] for i in range(0, len(body), 400)]
    wire = Wire(chunks)
    headers = [("host", f"127.0.0.1:{config.PORT}"), ("content-type", MULTIPART["content-type"]),
               ("content-length", "100")]  # announces 100 bytes, delivers 6000

    async def go():
        await asyncio.wait_for(app(scope(headers), wire.receive, wire.send), 5)

    asyncio.run(go())
    assert wire.status == 413
    assert meter.opened >= 1 and meter.written > 0  # the parser really had files open and had written to them
    assert meter.opened == meter.closed and folder_size(meter.dir) == 0  # and every one was closed and deleted
    assert repo.docs == {} and not list((storage_dir / ".uploading").glob("*"))


def test_a_client_that_hangs_up_in_the_middle_of_an_upload_leaves_nothing_behind(web, repo, meter, storage_dir,
                                                                                 monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 50 * MiB)
    total = multipart_length("yarim.pdf", 20 * MiB)
    result = post(web, {**MULTIPART, "Content-Length": total}, multipart_blocks("yarim.pdf", 20 * MiB),
                  limit_bytes=3 * MiB)  # sends 3 MB of the 20 MB it announced, then the connection closes
    assert result.sent >= 3 * MiB
    meter.wait_until_all_closed()  # the server notices the hang-up on its own time: wait for the closing, not a delay
    assert meter.opened == 1 and meter.opened == meter.closed and list(meter.dir.iterdir()) == []
    assert repo.docs == {} and list((storage_dir / ".uploading").iterdir()) == []



def test_uploads_at_the_same_time_are_each_judged_on_their_own(web, repo, meter, audit_limit):
    results = {}

    def upload(name, size, chunked):
        headers = dict(MULTIPART) if chunked else {**MULTIPART, "Content-Length": multipart_length(name, size)}
        results[name] = post(web, headers, multipart_blocks(name, size), chunked=chunked)

    jobs = [("big-1.pdf", 30 * MiB, False), ("big-2.pdf", 30 * MiB, True), ("big-3.pdf", 30 * MiB, False),
            ("ok-1.txt", 2000, False), ("ok-2.txt", 3000, True), ("ok-3.txt", 4000, False)]
    threads = [threading.Thread(target=upload, args=job) for job in jobs]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert {n: r.status for n, r in results.items()} == {
        "big-1.pdf": 413, "big-2.pdf": 411, "big-3.pdf": 413,  # too big (announced) / no length / too big
        "ok-1.txt": 200, "ok-2.txt": 411, "ok-3.txt": 200}  # fine / no length / fine
    assert set(repo.docs) == {"ok-1.txt", "ok-3.txt"}
    meter.wait_until_all_closed()  # the files are closed after the answers have gone out, not before them
    assert meter.opened == 2 == meter.closed  # exactly the two accepted uploads reached the parser, both were closed


def test_after_a_refusal_the_server_goes_on_serving_everything_else(web, repo, meter, audit_limit, monkeypatch):
    big = multipart_length("katta.pdf", 20 * MiB)
    post(web, {**MULTIPART, "Content-Length": big}, multipart_blocks("katta.pdf", 20 * MiB))
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", web, timeout=5)
    conn.request("GET", "/healthz")
    assert conn.getresponse().status == 200
    conn.close()
    small = {**MULTIPART, "Content-Length": multipart_length("c.txt", 100)}
    assert post(web, small, multipart_blocks("c.txt", 100)).status == 200
