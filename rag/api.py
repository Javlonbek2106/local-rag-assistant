"""Web API and the static front end. Listens on loopback only."""
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from rag import answer, config, ingest, store
from rag.worker import IndexWorker, list_files

log = logging.getLogger(__name__)


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


def create_app(worker=None):
    worker = worker or IndexWorker()
    upload_tmp = config.DOCS_DIR / ".uploading"
    allowed_hosts = {f"127.0.0.1:{config.PORT}", f"localhost:{config.PORT}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}

    @asynccontextmanager
    async def lifespan(_app):
        config.DOCS_DIR.mkdir(parents=True, exist_ok=True)
        upload_tmp.mkdir(exist_ok=True)
        for leftover in upload_tmp.iterdir():  # uploads interrupted by a crash
            leftover.unlink(missing_ok=True)
        worker.start()
        for path in list_files():  # restore the queue: unchanged files are recognised by hash and skipped fast
            worker.submit(path.name)
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

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

    def doc_path(name):
        clean = Path(name).name
        if not clean or clean != name or not ingest.is_supported(Path(clean)):
            raise HTTPException(400, "Fayl nomi yaroqsiz")
        return config.DOCS_DIR / clean

    @app.get("/api/documents")
    def documents():
        snapshot = worker.snapshot()
        rows = []
        for path in list_files():
            info = snapshot.get(path.name, {"state": "queued", "chunks": None, "error": None, "seconds": None})
            rows.append({"name": path.name, "size": path.stat().st_size, **info})
        return {"documents": rows, "queued": worker.queued(), "error": worker.error}

    @app.post("/api/documents")
    async def upload(files: list[UploadFile] = File(...)):
        results = []
        for upload_file in files:
            name = Path(upload_file.filename or "").name
            if not name or not ingest.is_supported(Path(name)):
                results.append({"name": name or "?", "accepted": False, "error": "Bu fayl turi qo'llab-quvvatlanmaydi"})
                continue
            if worker.is_processing(name):
                busy = "Fayl hozir o'qilmoqda, tugagach qayta yuklang"
                results.append({"name": name, "accepted": False, "error": busy})
                continue
            tmp = upload_tmp / uuid.uuid4().hex
            size = 0
            too_big = False
            with tmp.open("wb") as out:
                while chunk := await upload_file.read(1024 * 1024):
                    size += len(chunk)
                    if size > config.MAX_UPLOAD_BYTES:
                        too_big = True
                        break
                    out.write(chunk)
            if too_big or size == 0:
                tmp.unlink(missing_ok=True)
                error = f"Fayl {config.MAX_UPLOAD_BYTES // 1024 // 1024} MB dan katta" if too_big else "Fayl bo'sh"
                results.append({"name": name, "accepted": False, "error": error})
                continue
            os.replace(tmp, config.DOCS_DIR / name)  # atomic: the worker never sees a half-written file
            worker.submit(name)
            log.info("uploaded %s (%d bytes)", name, size)
            results.append({"name": name, "accepted": True})
        return {"results": results}

    @app.delete("/api/documents/{name}")
    def delete_document(name: str):
        path = doc_path(name)
        if worker.is_processing(name):
            raise HTTPException(409, "Fayl hozir o'qilmoqda, tugagach o'chiring")
        path.unlink(missing_ok=True)
        store.delete_source(store.get_client(), str(path))
        worker.forget(name)
        log.info("deleted %s", name)
        return {"deleted": name}

    @app.post("/api/ask")
    def ask(body: Question):
        try:
            hits = answer.retrieve(body.question)
        except Exception as error:
            log.exception("retrieval failed")
            raise HTTPException(503, f"Qidiruv ishlamadi (Ollama va Qdrant yoqilganmi?): {error}") from error

        def events():
            parts = []
            try:
                for piece in answer.stream_answer(body.question, hits):
                    parts.append(piece)
                    yield json.dumps({"token": piece}) + "\n"
            except Exception as error:
                log.exception("answer generation failed")
                yield json.dumps({"error": f"Javob yozilmadi: {error}"}) + "\n"
                return
            text = "".join(parts).strip()
            found = bool(hits) and answer.NOT_FOUND not in text
            sources = [
                {"n": i, "file": h.payload["file_name"], "pages": h.payload["pages"],
                 "score": round(h.score, 3), "text": h.payload["text"][:600]}
                for i, h in enumerate(hits, 1)
            ] if found else []
            yield json.dumps({"final": text if found else answer.NOT_FOUND, "found": found, "sources": sources}) + "\n"

        return StreamingResponse(events(), media_type="application/x-ndjson")

    @app.get("/")
    def index():
        return FileResponse(config.STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")
    return app
