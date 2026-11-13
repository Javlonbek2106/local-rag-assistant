"""Web API and the static front end. Listens on loopback only. Indexing happens in the worker process."""
import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from rag import answer, config, db, ingest, llm, storage, store, versioning

log = logging.getLogger(__name__)


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class Rollback(BaseModel):
    version: int = Field(ge=1)


def iso(moment):
    return moment.isoformat() if moment else None


def create_app(repo=db):
    """`repo` is the document/queue repository (rag.db); tests pass an in-memory fake."""
    allowed_hosts = {f"127.0.0.1:{config.PORT}", f"localhost:{config.PORT}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}

    @asynccontextmanager
    async def lifespan(_app):
        repo.migrate()
        storage.temp_dir().mkdir(parents=True, exist_ok=True)
        for leftover in storage.temp_dir().iterdir():  # uploads interrupted by a crash
            leftover.unlink(missing_ok=True)
        try:
            store.ensure_collection(store.get_client(), lambda: len(llm.embed(["probe"])[0]))
            versioning.backfill_legacy(repo, store.get_client())
        except Exception:  # noqa: BLE001 - Qdrant/Ollama may start later; asking will then report it clearly
            log.exception("could not prepare the index at startup")
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

    def valid_name(name):
        clean = Path(name).name
        if not clean or clean != name or not ingest.is_supported(Path(clean)):
            raise HTTPException(400, "Fayl nomi yaroqsiz")
        return clean

    @app.get("/api/documents")
    def documents():
        rows = [
            {"name": d["name"], "size": d["size_bytes"], "chunks": d["chunks"], "error": d["error"],
             "seconds": d["seconds"],
             # the version searches use now (None until the first one is ready), the newest uploaded one, and why
             # the newest failed while an older one keeps working
             "version": d["version"], "latest_version": d["latest_version"], "update_error": d["update_error"],
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
            {"version": v["version_number"], "status": v["status"], "active": bool(v["active"]),
             "size": v["file_size"], "chunks": v["chunk_count"], "error": v["error_message"],
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
    def ask(body: Question):
        try:
            hits = answer.retrieve(body.question, repo=repo)
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
                {"n": n, "file": h.payload["file_name"], "pages": h.payload["pages"],
                 "score": round(h.score, 3), "text": h.payload["text"][:600]}
                for n, h in answer.used_sources(text, hits)
            ] if found else []
            yield json.dumps({"final": text if found else answer.NOT_FOUND, "found": found, "sources": sources}) + "\n"

        return StreamingResponse(events(), media_type="application/x-ndjson")

    @app.get("/")
    def index():
        return FileResponse(config.STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")
    return app
