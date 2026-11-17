"""A fake Ollama server speaking real HTTP on a local port, for tests of rag/llm.py and the ask endpoint.

It behaves like the real one where it matters here: the response to a streamed chat starts with its first token (chunked
NDJSON), /api/version answers at once even while a chat is running, one chat at a time (`serial`: a second chat waits
for the first), and it notices when the client closes the connection (`closed_at`). The behaviour of the next chat is
set with plain attributes.
"""
import json
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    # -- helpers --
    def client_closed(self):
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

    def pause(self, seconds):
        """Wait, but give up early (False) when the client closes the connection or the server is stopping."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.client_closed():
                return False
            if self.server.fake.stopping.is_set():
                return False
            time.sleep(0.01)
        return True

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def chunk(self, body):
        data = (json.dumps(body) + "\n").encode()
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    # -- routes --
    def do_GET(self):
        fake = self.server.fake
        if self.path == "/api/version" and not fake.hang_everything:
            self.reply(200, {"version": "fake"})
        elif self.path == "/api/version":
            self.pause(60)
        else:
            self.reply(404, {"error": "not found"})

    def do_POST(self):
        fake = self.server.fake
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        fake.requests.append((self.path, body))
        if fake.hang_everything:
            self.pause(60)
        elif self.path == "/api/embed":
            self.embed(body)
        elif self.path == "/api/chat":
            with fake.serial_lock if fake.serial else fake.no_lock:
                self.chat(body)
        elif self.path == "/api/generate":
            self.reply(200, {"model": body.get("model"), "done": True})
        else:
            self.reply(404, {"error": "not found"})

    def embed(self, body):
        fake = self.server.fake
        if fake.embed_delay:
            self.pause(fake.embed_delay)
        if fake.embed_status != 200:
            return self.reply(fake.embed_status, {"error": "model not found"})
        inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
        self.reply(200, {"model": body["model"], "embeddings": [[0.1, 0.2, 0.3] for _ in inputs]})

    def chat(self, body):
        fake = self.server.fake
        fake.chat_started.set()
        fake.chat_started_at = time.monotonic()
        try:
            if fake.chat_status != 200:
                return self.reply(fake.chat_status, {"error": "model 'x' not found"})
            if fake.first_delay and not self.pause(fake.first_delay):
                return None
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            sent = 0
            while True:
                if fake.endless or sent < len(fake.tokens):
                    text = fake.tokens[sent % len(fake.tokens)]
                    self.chunk({"message": {"role": "assistant", "content": text}, "done": False})
                    sent += 1
                    if fake.stall_after is not None and sent >= fake.stall_after:
                        self.pause(60)  # silence: the model has hung
                        return None
                    if fake.error_after is not None and sent >= fake.error_after:
                        self.chunk({"error": "llama runner crashed"})
                        return None
                    if not self.pause(fake.token_delay):
                        return None
                else:
                    self.chunk({"message": {"role": "assistant", "content": ""}, "done": True,
                                "done_reason": fake.done_reason})
                    self.wfile.write(b"0\r\n\r\n")
                    return None
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return None
        finally:
            if self.client_closed():
                fake.mark_closed()
            fake.chat_finished.set()


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass  # a client that disconnects mid-response is an expected event here, not an error to print


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOllama:
    def __init__(self):
        self.requests = []
        self.tokens = ["Lola", "-5G ", "[1]"]
        self.token_delay = 0.0
        self.first_delay = 0.0
        self.endless = False
        self.stall_after = None
        self.error_after = None
        self.done_reason = "stop"
        self.chat_status = 200
        self.embed_status = 200
        self.embed_delay = 0.0
        self.hang_everything = False
        self.serial = True
        self.serial_lock = threading.Lock()
        self.no_lock = _NoLock()
        self.stopping = threading.Event()
        self.chat_started = threading.Event()
        self.chat_finished = threading.Event()
        self.chat_started_at = None
        self.closed_at = None
        self.closed = threading.Event()
        self.server = _Server(("127.0.0.1", 0), _Handler)
        self.server.fake = self
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def mark_closed(self):
        if self.closed_at is None:
            self.closed_at = time.monotonic()
            self.closed.set()

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.stopping.set()
        self.server.shutdown()
        self.server.server_close()

    def chats(self):
        return [body for path, body in self.requests if path == "/api/chat"]
