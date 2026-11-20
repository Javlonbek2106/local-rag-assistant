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
# Hardened PostgreSQL (roadmap M4.1a, step 7, docs/postgres-hardening.md): the application connects as three different
# roles (runtime / migrations / bootstrap), each with its own password kept in a private secret file, never in a URL.
# Off: the single-role mode above, unchanged (RAG_DATABASE_URL). The connector refuses to run unless it is on.
DB_HARDENED = os.getenv("RAG_DB_HARDENED", "").lower() in ("1", "true")
DB_HOST = os.getenv("RAG_DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("RAG_DB_PORT", "5433"))
DB_NAME = os.getenv("RAG_DB_NAME", "rag")

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


# Connector security foundation (roadmap M4.1a, ADR-P16). Everything below is inert while CONNECTOR_ENABLED is false:
# no route is added and no check runs, so the existing application behaves exactly as before (decision D-15).
CONNECTOR_ENABLED = os.getenv("RAG_CONNECTOR_ENABLED", "0").strip().lower() in ("1", "true")


def _default_state_dir():
    """Where the connector keeps its secrets and the security log: outside the project, in the user's own profile."""
    if os.name == "nt":
        base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.getenv("XDG_STATE_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "local-rag-assistant"


STATE_DIR = Path(os.getenv("RAG_STATE_DIR") or _default_state_dir())
SECRETS_DIR = Path(os.getenv("RAG_SECRETS_DIR") or STATE_DIR / "secrets")
SECURITY_LOG_DIR = Path(os.getenv("RAG_SECURITY_LOG_DIR") or STATE_DIR / "logs" / "security")
# Accepted values (D-4), in seconds. Deliberately not environment settings: a longer session must be a decision.
SESSION_ABSOLUTE_S = 12 * 60  # a session ends this long after it was made, whatever happens
SESSION_IDLE_S = 2 * 60  # ... or after this long without a request that needs it
STEP_UP_S = 10 * 60  # a step-up confirmation counts this long, never past the end of the session
SESSION_MAX_COUNT = 5  # sessions alive at once; the oldest is ended when a sixth is made
# One-time pairing code (D-3). The numbers are implementation parameters proposed in the M4.1a plan, to be confirmed.
PAIRING_DIGITS = 8
PAIRING_TTL_S = 60
PAIRING_REQUEST_GAP_S = 10  # a new code cannot be asked for sooner than this after the previous request
PAIRING_ATTEMPTS = 5  # exchange attempts allowed per PAIRING_WINDOW_S; the next one starts a lockout
PAIRING_WINDOW_S = 60
PAIRING_LOCKOUT_S = 30
# Security event log (D-14): rotated by size. Retention is by file count here; a time limit is still an open decision.
SECURITY_LOG_MAX_BYTES = 5 * 1024 * 1024
SECURITY_LOG_BACKUPS = 5
SECURITY_CHECK_TTL_S = 60  # how long the result of the security checks is reused
SECURITY_DB_TIMEOUT_S = 5  # how long a security check waits for the database (the pool's own wait is 30 s)

# External database connector (roadmap M4.1b, docs/connector.md). Inert while CONNECTOR_ENABLED is false.
ENCRYPTION_KEY_FILE = "encryption.key"  # in SECRETS_DIR, outside PostgreSQL/backups/repo/image
CONNECTOR_MAX_CONNECTIONS = int(os.getenv("RAG_CONNECTOR_MAX_CONNECTIONS", "50"))  # per owner
CONNECTOR_TEST_TIMEOUT_S = float(os.getenv("RAG_CONNECTOR_TEST_TIMEOUT_S", "10"))  # one test, connecting included
CONNECTOR_DISCOVERY_TIMEOUT_S = float(os.getenv("RAG_CONNECTOR_DISCOVERY_TIMEOUT_S", "60"))  # one whole discovery
CONNECTOR_MAX_CONCURRENT = int(os.getenv("RAG_CONNECTOR_MAX_CONCURRENT", "4"))  # external operations at once
CONNECTOR_MAX_OBJECTS = int(os.getenv("RAG_CONNECTOR_MAX_OBJECTS", "20000"))  # tables + views one discovery stores
CONNECTOR_MAX_COLUMNS = int(os.getenv("RAG_CONNECTOR_MAX_COLUMNS", "300000"))
# Optional: only these hosts may be connected to (comma separated; "*.example.org" allowed). Empty: any host that the
# fixed rules of rag/connector/network.py do not block (link-local, unspecified, multicast, our own database).
_ALLOWED = os.getenv("RAG_CONNECTOR_ALLOWED_HOSTS", "")
CONNECTOR_ALLOWED_HOSTS = tuple(h.strip().lower() for h in _ALLOWED.split(",") if h.strip())

# Schema snapshots and context (roadmap M4.2, docs/connector.md).
CONNECTOR_SNAPSHOT_RETENTION = max(2, int(os.getenv("RAG_CONNECTOR_SNAPSHOT_RETENTION", "10")))  # snapshots kept
CONNECTOR_SCHEMA_MAX_AGE_S = float(os.getenv("RAG_CONNECTOR_SCHEMA_MAX_AGE_S", str(7 * 24 * 3600)))  # older = stale
CONNECTOR_MAX_CHANGES = int(os.getenv("RAG_CONNECTOR_MAX_CHANGES", "5000"))  # kept per snapshot
CONNECTOR_MAX_SELECTION_ITEMS = int(os.getenv("RAG_CONNECTOR_MAX_SELECTION_ITEMS", "50000"))
CONNECTOR_CONTEXT_MAX_CHARS = int(os.getenv("RAG_CONNECTOR_CONTEXT_MAX_CHARS", "24000"))  # the default budget
CONNECTOR_CONTEXT_HARD_MAX_CHARS = int(os.getenv("RAG_CONNECTOR_CONTEXT_HARD_MAX_CHARS", "200000"))  # the largest ask

# Natural language -> SQL (roadmap M4.3, docs/sql-generation.md). Generation only: nothing here runs SQL.
# The model is served by the same Ollama as the answers; an empty RAG_SQL_MODEL means "the answer model" (LLM_MODEL).
SQL_MODEL = os.getenv("RAG_SQL_MODEL", "") or LLM_MODEL
SQL_NUM_CTX = int(os.getenv("RAG_SQL_NUM_CTX", str(LLM_NUM_CTX)))  # the model's context window for one generation
SQL_NUM_PREDICT = int(os.getenv("RAG_SQL_NUM_PREDICT", "768"))  # the most tokens one generated answer may have
SQL_CONTEXT_MAX_CHARS = int(os.getenv("RAG_SQL_CONTEXT_MAX_CHARS", "12000"))  # the schema part of the prompt, at most
SQL_PROMPT_MAX_CHARS = int(os.getenv("RAG_SQL_PROMPT_MAX_CHARS", "16000"))  # the whole prompt, rules included
SQL_MAX_OUTPUT_CHARS = int(os.getenv("RAG_SQL_MAX_OUTPUT_CHARS", "8000"))  # a longer generated query is refused
SQL_MAX_CONCURRENT = int(os.getenv("RAG_SQL_MAX_CONCURRENT", "2"))  # generations at once (the model does one at a time)
SQL_CHARS_PER_TOKEN = float(os.getenv("RAG_SQL_CHARS_PER_TOKEN", "2.5"))  # cautious: prompt + answer must fit
