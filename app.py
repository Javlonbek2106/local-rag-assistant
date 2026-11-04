"""Local web app: upload documents, watch their indexing status, ask questions.

Run: python app.py   ->  http://127.0.0.1:8765
Listens on localhost only. Files are indexed one at a time in a background worker.
"""
import json
import os
import queue
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from qdrant_client import QdrantClient, models

import answer
import ingest

BASE = Path(__file__).resolve().parent
DOCS_DIR = Path(os.getenv("RAG_DOCS", BASE / "documents")).resolve()
UPLOAD_TMP = DOCS_DIR / ".uploading"
STATIC_DIR = BASE / "static"
HOST = "127.0.0.1"
PORT = int(os.getenv("RAG_PORT", "8765"))
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ALLOWED_ORIGINS = {f"http://{h}" for h in ALLOWED_HOSTS}

qdrant = QdrantClient(url=ingest.QDRANT_URL)
jobs: "queue.Queue[str]" = queue.Queue()
status: dict[str, dict] = {}  # file name -> {"state", "chunks", "error"}
status_lock = threading.Lock()
worker_error: str | None = None


def set_status(name, state, chunks=None, error=None):
    with status_lock:
        status[name] = {"state": state, "chunks": chunks, "error": error}


def stored_chunks(path):
    return qdrant.count(ingest.COLLECTION, count_filter=ingest.source_filter(str(path)), exact=True).count


def remove_from_index(path):
    qdrant.delete(ingest.COLLECTION, points_selector=models.FilterSelector(filter=ingest.source_filter(str(path))))


def worker():
    global worker_error
    try:
        client = ingest.open_collection()
        converter, chunker = ingest.build_pipeline()
    except Exception as error:  # Qdrant or Ollama not running, Tesseract missing, ...
        worker_error = f"Indekslash xizmati ishga tushmadi: {error}"
        return
    while True:
        name = jobs.get()
        path = DOCS_DIR / name
        if not path.exists():  # deleted while waiting in the queue
            continue
        set_status(name, "processing")
        try:
            count = ingest.index_file(client, converter, chunker, path)
            if not path.exists():  # deleted while being indexed: drop what was just written
                remove_from_index(path)
                continue
            set_status(name, "done", chunks=stored_chunks(path) if count is None else count)
        except Exception as error:
            set_status(name, "failed", error=str(error)[:300])


def list_files():
    return sorted(p for p in DOCS_DIR.iterdir() if p.is_file() and ingest.is_supported(p))


@asynccontextmanager
async def lifespan(_app):
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_TMP.mkdir(exist_ok=True)
    for leftover in UPLOAD_TMP.iterdir():  # uploads interrupted by a crash
        leftover.unlink(missing_ok=True)
    threading.Thread(target=worker, daemon=True).start()
    for path in list_files():  # restore the queue: unchanged files are recognised by hash and skipped fast
        set_status(path.name, "queued")
        jobs.put(path.name)
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def guard(request: Request, call_next):
    # Host check blocks DNS-rebinding; Origin check blocks other websites from posting to us.
    if request.headers.get("host") not in ALLOWED_HOSTS:
        return JSONResponse({"error": "host not allowed"}, status_code=403)
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD") and origin is not None and origin not in ALLOWED_ORIGINS:
        return JSONResponse({"error": "origin not allowed"}, status_code=403)
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def doc_path(name):
    clean = Path(name).name
    if not clean or clean != name or not ingest.is_supported(Path(clean)):
        raise HTTPException(400, "Fayl nomi yaroqsiz")
    return DOCS_DIR / clean


@app.get("/api/documents")
def documents():
    with status_lock:
        snapshot = dict(status)
    rows = []
    for path in list_files():
        info = snapshot.get(path.name, {"state": "queued", "chunks": None, "error": None})
        rows.append({"name": path.name, "size": path.stat().st_size, **info})
    return {"documents": rows, "queued": jobs.qsize(), "error": worker_error}


@app.post("/api/documents")
async def upload(files: list[UploadFile] = File(...)):
    results = []
    for upload_file in files:
        name = Path(upload_file.filename or "").name
        if not name or not ingest.is_supported(Path(name)):
            results.append({"name": name or "?", "accepted": False, "error": "Bu fayl turi qo'llab-quvvatlanmaydi"})
            continue
        tmp = UPLOAD_TMP / uuid.uuid4().hex
        size = 0
        too_big = False
        with tmp.open("wb") as out:
            while chunk := await upload_file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    too_big = True
                    break
                out.write(chunk)
        if too_big or size == 0:
            tmp.unlink(missing_ok=True)
            error = "Fayl 200 MB dan katta" if too_big else "Fayl bo'sh"
            results.append({"name": name, "accepted": False, "error": error})
            continue
        os.replace(tmp, DOCS_DIR / name)  # atomic: the worker never sees a half-written file
        set_status(name, "queued")
        jobs.put(name)
        results.append({"name": name, "accepted": True})
    return {"results": results}


@app.delete("/api/documents/{name}")
def delete_document(name: str):
    path = doc_path(name)
    path.unlink(missing_ok=True)
    remove_from_index(path)
    with status_lock:
        status.pop(name, None)
    return {"deleted": name}


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


@app.post("/api/ask")
def ask(body: Question):
    try:
        hits = answer.retrieve(body.question, qdrant)
    except Exception as error:
        raise HTTPException(503, f"Qidiruv ishlamadi (Ollama va Qdrant yoqilganmi?): {error}")

    def events():
        parts = []
        try:
            for piece in answer.stream_answer(body.question, hits):
                parts.append(piece)
                yield json.dumps({"token": piece}) + "\n"
        except Exception as error:
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
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
