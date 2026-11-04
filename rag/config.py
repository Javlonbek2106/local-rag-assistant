"""Every setting in one place. Any value can be overridden with an RAG_* environment variable.

Other modules read these as `config.NAME` at call time (never `from rag.config import NAME`),
so tests can change a value with monkeypatch.
"""
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Vector store
QDRANT_URL = os.getenv("RAG_QDRANT_URL", "http://127.0.0.1:6333")
COLLECTION = os.getenv("RAG_COLLECTION", "documents")

# Models (served by Ollama; the Ollama client reads OLLAMA_HOST itself)
EMBED_MODEL = os.getenv("RAG_EMBED_MODEL", "bge-m3")
TOKENIZER = os.getenv("RAG_TOKENIZER", "BAAI/bge-m3")
LLM_MODEL = os.getenv("RAG_LLM", "qwen2.5:7b")
LLM_NUM_CTX = int(os.getenv("RAG_LLM_NUM_CTX", "8192"))

# Retrieval
TOP_K = int(os.getenv("RAG_TOP_K", "5"))
# Coarse pre-filter only. Measured on a small test set, unrelated questions scored up to 0.458 while the
# weakest correct hit scored 0.478, so this cannot separate them reliably; the LLM prompt is the second guard.
# To be calibrated on a real evaluation set (plan, stage 1).
MIN_SCORE = float(os.getenv("RAG_MIN_SCORE", "0.40"))

# Indexing
MAX_CHUNK_TOKENS = int(os.getenv("RAG_MAX_CHUNK_TOKENS", "512"))
EMBED_BATCH = int(os.getenv("RAG_EMBED_BATCH", "32"))
DOCS_DIR = Path(os.getenv("RAG_DOCS", ROOT / "documents")).resolve()
MAX_UPLOAD_BYTES = int(os.getenv("RAG_MAX_UPLOAD_MB", "200")) * 1024 * 1024
EXTENSIONS = frozenset({
    ".pdf", ".docx", ".pptx", ".xlsx", ".html", ".htm", ".md", ".csv", ".txt",
    ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp",
})

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
