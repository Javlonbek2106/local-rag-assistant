"""Every setting in one place. Any value can be overridden with an RAG_* environment variable.

Other modules read these as `config.NAME` at call time (never `from rag.config import NAME`),
so tests can change a value with monkeypatch.
"""
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Vector store. "documents_v2": chunks are keyed by document id (v1 used file paths and is no longer read).
QDRANT_URL = os.getenv("RAG_QDRANT_URL", "http://127.0.0.1:6333")
COLLECTION = os.getenv("RAG_COLLECTION", "documents_v2")

# PostgreSQL: the durable record of documents and the indexing queue
DATABASE_URL = os.getenv("RAG_DATABASE_URL", "postgresql://rag:rag@127.0.0.1:5433/rag")
DB_SCHEMA = os.getenv("RAG_DB_SCHEMA")  # tests only: isolates a run in its own schema
DB_POOL_SIZE = int(os.getenv("RAG_DB_POOL_SIZE", "5"))

# Indexing jobs
JOB_MAX_ATTEMPTS = int(os.getenv("RAG_JOB_MAX_ATTEMPTS", "3"))
# A job is killed when its progress (page, chunk, stage) has not moved for JOB_STALL_S: a big document may take hours
# as long as it keeps moving. JOB_TIMEOUT_S is only the absolute ceiling against a job that "moves" forever.
JOB_STALL_S = int(os.getenv("RAG_JOB_STALL_S", "600"))
JOB_TIMEOUT_S = int(os.getenv("RAG_JOB_TIMEOUT_S", "21600"))
JOB_HEARTBEAT_S = int(os.getenv("RAG_JOB_HEARTBEAT_S", "15"))
JOB_STALE_AFTER_S = int(os.getenv("RAG_JOB_STALE_AFTER_S", "90"))  # no heartbeat for this long: worker is dead
WORKER_POLL_S = float(os.getenv("RAG_WORKER_POLL_S", "1"))  # how often an idle worker looks for new documents

# Models (served by Ollama at OLLAMA_HOST, default http://127.0.0.1:11434; see rag/llm.py)
EMBED_MODEL = os.getenv("RAG_EMBED_MODEL", "bge-m3")
TOKENIZER = os.getenv("RAG_TOKENIZER", "BAAI/bge-m3")
LLM_MODEL = os.getenv("RAG_LLM", "qwen2.5:7b")
LLM_NUM_CTX = int(os.getenv("RAG_LLM_NUM_CTX", "8192"))
# One answer is at most this many tokens (about 100 s on this CPU); a longer one is cut and the page says so.
LLM_NUM_PREDICT = int(os.getenv("RAG_LLM_NUM_PREDICT", "512"))
# How long Ollama keeps a model in memory after its last use. Loading qwen2.5:7b takes about 28 s on this CPU, so
# a model that stays loaded makes the next question fast. "-1" keeps it forever, "0" unloads it at once.
OLLAMA_KEEP_ALIVE = os.getenv("RAG_OLLAMA_KEEP_ALIVE", "30m")
WARMUP = os.getenv("RAG_WARMUP", "1") == "1"  # load both models when the web server starts
# Timeouts of every call to Ollama. The answer's first token can legitimately take over 30 s here (loading the model,
# reading a 4000-token prompt), and the response only starts with it, so a slow Ollama is told from a dead one by
# asking it a cheap question (/api/version) while waiting: no reply to the request nor to that for
# OLLAMA_UNRESPONSIVE_S means Ollama is down, and the caller gets an error then, not after the long limits below.
OLLAMA_CONNECT_TIMEOUT_S = float(os.getenv("RAG_OLLAMA_CONNECT_TIMEOUT_S", "5"))
OLLAMA_PROBE_INTERVAL_S = float(os.getenv("RAG_OLLAMA_PROBE_INTERVAL_S", "5"))
OLLAMA_UNRESPONSIVE_S = float(os.getenv("RAG_OLLAMA_UNRESPONSIVE_S", "20"))
EMBED_TIMEOUT_S = float(os.getenv("RAG_EMBED_TIMEOUT_S", "120"))  # one embedding request, alive but slow
LLM_FIRST_TOKEN_TIMEOUT_S = float(os.getenv("RAG_LLM_FIRST_TOKEN_TIMEOUT_S", "120"))
LLM_IDLE_TIMEOUT_S = float(os.getenv("RAG_LLM_IDLE_TIMEOUT_S", "30"))  # no token for this long once it has started
LLM_TOTAL_TIMEOUT_S = float(os.getenv("RAG_LLM_TOTAL_TIMEOUT_S", "300"))  # one whole answer

# Retrieval
TOP_K = int(os.getenv("RAG_TOP_K", "5"))
# Coarse pre-filter only. Measured on a small test set, unrelated questions scored up to 0.458 while the
# weakest correct hit scored 0.478, so this cannot separate them reliably; the LLM prompt is the second guard.
# To be calibrated on a real evaluation set (plan, stage 1).
MIN_SCORE = float(os.getenv("RAG_MIN_SCORE", "0.40"))
# Cross-encoder that re-orders the vector hits (rag/rerank.py), e.g. "BAAI/bge-reranker-v2-m3". Off by default.
# Measured with qwen2.5:7b on 78 real questions: right passage at rank 1 70% -> 83%, but answers did not improve
# (fact in answer 80% -> 78%) and it costs about 10 s per question on a CPU. Worth re-checking with a stronger model.
RERANK_MODEL = os.getenv("RAG_RERANK_MODEL", "")
RERANK_CANDIDATES = int(os.getenv("RAG_RERANK_CANDIDATES", "15"))

# Indexing
MAX_CHUNK_TOKENS = int(os.getenv("RAG_MAX_CHUNK_TOKENS", "512"))
TABLE_FORMAT = os.getenv("RAG_TABLE_FORMAT", "triplet")  # "triplet" or "markdown", see ingest.build_chunker
EMBED_BATCH = int(os.getenv("RAG_EMBED_BATCH", "32"))
# Uploaded files: <STORAGE_DIR>/<document id>/<original name>. The old `documents/` folder is no longer used.
STORAGE_DIR = Path(os.getenv("RAG_STORAGE", ROOT / "storage")).resolve()
MAX_UPLOAD_BYTES = int(os.getenv("RAG_MAX_UPLOAD_MB", "200")) * 1024 * 1024  # the size limit of ONE uploaded file
# What a whole upload request may carry on top of that: multipart boundaries and part headers (rag/bodylimit.py)
UPLOAD_FRAMING_BYTES = 1024 * 1024
EXTENSIONS = frozenset({
    ".pdf", ".docx", ".pptx", ".xlsx", ".html", ".htm", ".md", ".csv", ".txt",
    ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp",
})

# Operations: where `python -m rag backup` puts its backups and where `reconcile` keeps its last report.
# Both folders hold private data / state: they are git-ignored and never part of a backup.
BACKUP_DIR = Path(os.getenv("RAG_BACKUP_DIR", ROOT / "backups"))
REPORT_DIR = Path(os.getenv("RAG_REPORT_DIR", ROOT / "reports"))

# OCR
TESSERACT = (
    os.getenv("RAG_TESSERACT")
    or shutil.which("tesseract")
    or r"C:\Program Files\Tesseract-OCR\tesseract.exe"
)
TESSDATA = Path(os.getenv("RAG_TESSDATA", ROOT / "tessdata"))
OCR_LANGS = ["uzb", "uzb_cyrl", "rus", "eng"]

# Web server. Loopback only on purpose: there is no login yet (plan, stage 3).
HOST = "127.0.0.1"
PORT = int(os.getenv("RAG_PORT", "8765"))
STATIC_DIR = ROOT / "static"
