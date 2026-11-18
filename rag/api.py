"""Web API and the static front end. Listens on loopback only. Indexing happens in the worker process."""
import asyncio
import json
import logging
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from rag import answer, config, db, health, ingest, llm, storage, store, versioning
from rag.auth.deps import build_context as build_security
from rag.auth.routes import install as install_auth
from rag.auth.routes import install_discovery
from rag.bodylimit import UploadBodyLimit
from rag.relay import Relay

log = logging.getLogger(__name__)


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class Rollback(BaseModel):
    version: int = Field(ge=1)


def iso(moment):
    return moment.isoformat() if moment else None


# What a version is doing, in the words the page shows. One mapping, here, so the page never guesses from raw statuses.
PHASES = {"READY": "active", "SUPERSEDED": "superseded", "FAILED": "failed", "UPLOADED": "queued", "QUEUED": "queued",
          "PROCESSING": "processing", "EMBEDDING": "processing", "INDEXING": "processing", "VERIFYING": "processing"}


def phase(version):
    return PHASES[version["status"]]


class OwnedStream(StreamingResponse):
    """A streaming response that runs `on_close` when it ends *or is cancelled*.

    Cleanup in the body generator's `finally` is not enough: when the client hangs up, the task that is sending the
    response is cancelled while the generator is suspended at a `yield`, and Python finalizes such a generator only when
    it is garbage collected, which can be late. Measured on this project: the abandoned answer then kept running and
    the next question waited behind it. The cancellation passes through this method's own frame, so its `finally` runs
    at once, every time.
    """

    def __init__(self, content, on_close, **kwargs):
        super().__init__(content, **kwargs)
        self._on_close = on_close

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._on_close()


class ClientGone(Exception):
    """The browser (or other client) closed the connection while a question was being answered."""


def llm_error_code(error):
    """The machine-readable code of an Ollama failure ("llm_timeout", "llm_unavailable", "llm_error"), else None."""
    if isinstance(error, llm.LLMTimeout):
        return "llm_timeout"
    if isinstance(error, llm.LLMUnavailable):
        return "llm_unavailable"
    return "llm_error" if isinstance(error, llm.LLMError) else None


def progress_view(row):
    """Where a running version is, as the page shows it: {"unit": pages | assembling | chunks, "done", "total"}
    ("total" is null while it is not known). None when the worker has not reported a position in the current stage."""
    unit = row.get("progress_unit")
    return None if unit is None else {"unit": unit, "done": row["progress_done"], "total": row.get("progress_total")}


def stale(version):
    """Still waiting or being indexed, but uploaded before the latest rollback: it will be kept, never made active."""
    unfinished = phase(version) in ("queued", "processing")
    return unfinished and version["uploaded_generation"] < version["activation_generation"]


def create_app(repo=db, security=None):
    """`repo` is the document/queue repository (rag.db); tests pass an in-memory fake. `security` is the authentication
    layer of the connector (rag.auth): built here from the settings when the connector is enabled, passed in by tests;
    without it the application is exactly as it was before the connector (ADR-007, decisions D-1 and D-15)."""
    if security is None and config.CONNECTOR_ENABLED:
        security = build_security()
    allowed_hosts = {f"127.0.0.1:{config.PORT}", f"localhost:{config.PORT}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}

    @asynccontextmanager
    async def lifespan(_app):
        repo.migrate()
        if security:
            security.startup()  # never raises: a failed security condition makes the connector unavailable, no more
        storage.temp_dir().mkdir(parents=True, exist_ok=True)
        for leftover in storage.temp_dir().iterdir():  # uploads interrupted by a crash
            leftover.unlink(missing_ok=True)
        try:
            store.ensure_collection(store.get_client(), lambda: len(llm.embed(["probe"])[0]))
            versioning.backfill_legacy(repo, store.get_client())
        except Exception:  # noqa: BLE001 - Qdrant/Ollama may start later; asking will then report it clearly
            log.exception("could not prepare the index at startup")
        if config.WARMUP:  # load the models in the background: the first question should not wait for it
            threading.Thread(target=llm.warm_up, daemon=True, name="warm-up").start()
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    if security:
        install_auth(app, security)  # the connector's own routes, under /api/v1; no existing route is touched
    else:
        install_discovery(app)  # the one route that says the connector is off
    # Added before `guard`, so it sits inside it: the Host/Origin checks answer first, then the body is limited.
    app.add_middleware(UploadBodyLimit)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # Host check blocks DNS-rebinding; Origin check blocks other websites from posting to us.
        if request.headers.get("host") not in allowed_hosts:
            return JSONResponse({"error": "host not allowed"}, status_code=403)
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD") and origin is not None and origin not in allowed_origins:
            return JSONResponse({"error": "origin not allowed"}, status_code=403)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def valid_name(name):
        clean = Path(name).name
        if not clean or clean != name or not ingest.is_supported(Path(clean)):
            raise HTTPException(400, "Fayl nomi yaroqsiz")
        return clean

    @app.get("/healthz")
    def healthz():
        """Liveness: the process answers. No dependency is touched (see rag/health.py)."""
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        """Readiness: PostgreSQL and Qdrant answer (503 otherwise); without Ollama the answer is "degraded"."""
        result = health.readiness(repo, connector=security.readiness_word if security else None)
        return JSONResponse(result, status_code=503 if result["status"] == "not_ready" else 200)

    @app.get("/api/status")
    def status():
        """A compact summary: counts, queue age, what is being indexed, last errors, dependencies, last reconcile."""
        return health.status_report(repo, connector=security.readiness_word if security else None)

    @app.get("/api/documents")
    def documents():
        rows = [
            {"name": d["name"], "size": d["size_bytes"], "chunks": d["chunks"], "error": d["error"],
             "seconds": d["seconds"],
             # what the version being indexed is doing: its stage (PROCESSING, EMBEDDING, INDEXING, VERIFYING) and
             # position (pages parsed, chunks cut / embedded / written); null while nothing is being indexed
             "stage": d.get("stage"), "progress": progress_view(d),
             # the version searches use now (None until the first one is ready), the newest uploaded one, and why
             # the newest failed while an older one keeps working
             "version": d["version"], "latest_version": d["latest_version"], "update_error": d["update_error"],
             # true whenever a version is active, whatever else is going on: questions are answered from it
             "searchable": d["version"] is not None,
             # The page does not distinguish "still uploading" from "waiting for a worker".
             "state": "queued" if d["state"] == "uploading" else d["state"]}
            for d in repo.list_documents()
        ]
        return {"documents": rows, "queued": repo.queue_length(), "error": None}

    @app.post("/api/documents")
    async def upload(files: list[UploadFile] = File(...)):
        results = []
        for upload_file in files:
            name = Path(upload_file.filename or "").name
            if not name or not ingest.is_supported(Path(name)):
                results.append({"name": name or "?", "accepted": False, "error": "Bu fayl turi qo'llab-quvvatlanmaydi"})
                continue
            temp = storage.temp_dir() / uuid.uuid4().hex
            size = 0
            too_big = False
            with temp.open("wb") as out:
                while chunk := await upload_file.read(1024 * 1024):
                    size += len(chunk)
                    if size > config.MAX_UPLOAD_BYTES:
                        too_big = True
                        break
                    out.write(chunk)
            if too_big or size == 0:
                temp.unlink(missing_ok=True)
                error = f"Fayl {config.MAX_UPLOAD_BYTES // 1024 // 1024} MB dan katta" if too_big else "Fayl bo'sh"
                results.append({"name": name, "accepted": False, "error": error})
                continue
            version = ingest.queue_file(name, temp, size, repo)
            if version["created"]:
                log.info("uploaded %s as version %d (%d bytes)", name, version["version_number"], size)
            results.append({"name": name, "accepted": True, "version": version["version_number"],
                            "unchanged": not version["created"]})
        return {"results": results}

    @app.delete("/api/documents/{name}")
    def delete_document(name: str):
        name = valid_name(name)
        try:
            doc_id = repo.delete_document(name)
        except db.Busy:
            raise HTTPException(409, "Fayl hozir o'qilmoqda, tugagach o'chiring") from None
        if doc_id is not None:
            store.delete_source(store.get_client(), str(doc_id))
            storage.remove(doc_id)
            log.info("deleted %s", name)
        return {"deleted": name}

    @app.get("/api/documents/{name}/versions")
    def versions(name: str):
        found = repo.list_versions(valid_name(name))
        if found is None:
            raise HTTPException(404, "Hujjat topilmadi")
        return {"name": name, "versions": [
            {"version": v["version_number"], "status": v["status"], "phase": phase(v), "stale": stale(v),
             "active": bool(v["active"]),
             "size": v["file_size"], "chunks": v["chunk_count"], "error": v["error_message"],
             "progress": progress_view(v),
             "created_at": iso(v["created_at"]), "indexed_at": iso(v["indexed_at"]),
             "activated_at": iso(v["activated_at"])}
            for v in found]}

    @app.post("/api/documents/{name}/rollback")
    def rollback(name: str, body: Rollback):
        try:
            restored = versioning.rollback(repo, store.get_client(), valid_name(name), body.version)
        except versioning.RollbackError as error:
            raise HTTPException(error.status, str(error)) from error
        except Exception as error:
            log.exception("rollback failed")
            raise HTTPException(503, f"Qaytarib bo'lmadi (Qdrant yoqilganmi?): {error}") from error
        return {"name": name, "version": restored["version_number"], "active": True}

    @app.post("/api/ask")
    async def ask(request: Request, body: Question):
        """Retrieval, then the answer streamed as it is written (one JSON object per line).

        Retrieval and generation run in a Relay thread. A failure before the first token is a real HTTP error (503, or
        504 when Ollama was too slow); one after it is an {"error", "code"} line, as the status is already sent. If the
        client goes away the Relay is cancelled, which closes the connection to Ollama and frees it within moments.
        """
        def produce():
            hits = answer.retrieve(body.question, repo=repo)
            yield "hits", hits
            for piece in answer.stream_answer(body.question, hits):
                yield "token", piece

        relay = Relay(produce).start()
        gone = asyncio.Event()

        async def watch_client():
            # Until the response starts, nothing else listens for the client hanging up (and Starlette's
            # request.is_disconnected() does not make uvicorn read the socket); once it has started, Starlette does.
            while (await request.receive())["type"] != "http.disconnect":
                pass
            log.info("ask: the client disconnected (seen while the answer was being prepared)")
            gone.set()

        watcher = asyncio.create_task(watch_client())

        async def next_event():
            while True:
                event = await relay.get(0.25)
                if event:
                    return event
                if gone.is_set():
                    raise ClientGone

        hits, first, failure, ended = None, None, None, False
        try:
            while first is None and failure is None and not ended:  # until the first token (or the end, or an error)
                kind, value = await next_event()
                if kind == "end":
                    ended = True
                elif kind == "error" and (hits is None or llm_error_code(value)):
                    log.warning("ask failed before the first token: %s", value)
                    message = (f"Qidiruv ishlamadi (Ollama va Qdrant yoqilganmi?): {value}" if hits is None
                               else f"Javob yozilmadi: {value}")
                    code = llm_error_code(value)
                    return JSONResponse({"detail": message, **({"code": code} if code else {})},
                                        status_code=504 if code == "llm_timeout" else 503)
                elif kind == "error":
                    failure = value
                elif value[0] == "hits":
                    hits = value[1]
                else:
                    first = value[1]
        except ClientGone:
            relay.cancel()
            log.info("ask: the client went away before the answer started; stopped")
            return Response(status_code=499)
        except BaseException:
            relay.cancel()
            raise
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

        async def events():
            parts = []
            try:
                piece, error = first, failure
                while error is None:
                    if piece is not None:
                        parts.append(piece)
                        yield json.dumps({"token": piece}) + "\n"
                    if ended:
                        break
                    kind, value = await next_event()
                    piece = None
                    if kind == "end":
                        break
                    if kind == "error":
                        error = value
                    else:
                        piece = value[1]
                if error is not None:
                    log.warning("answer generation failed: %s", error, exc_info=not llm_error_code(error))
                    code = llm_error_code(error)
                    yield json.dumps({"error": f"Javob yozilmadi: {error}", **({"code": code} if code else {})}) + "\n"
                    return
                text = "".join(parts).strip()
                found = bool(hits) and answer.NOT_FOUND not in text
                sources = [
                    {"n": n, "file": h.payload["file_name"], "pages": h.payload["pages"],
                     "score": round(h.score, 3), "text": h.payload["text"][:600]}
                    for n, h in answer.used_sources(text, hits)
                ] if found else []
                final = {"final": text if found else answer.NOT_FOUND, "found": found, "sources": sources}
                if relay.scope.truncated:  # the answer reached the output limit (LLM_NUM_PREDICT) and was cut
                    final["truncated"] = True
                yield json.dumps(final) + "\n"
            except ClientGone:
                log.info("ask: the client went away after %d pieces; stopped", len(parts))
            finally:
                log.info("ask: the answer stream ended; cancelling the relay (%d pieces sent)", len(parts))
                relay.cancel()  # also when Starlette cancels this generator because the client disconnected

        return OwnedStream(events(), on_close=lambda: (relay.cancel(), watcher.cancel()),
                           media_type="application/x-ndjson")

    @app.get("/")
    def index():
        return FileResponse(config.STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")
    return app
