"""Read every supported document in a folder, chunk it and store it in Qdrant.

Usage: python ingest.py <folder>

Unchanged files are skipped, changed files are replaced. Everything runs locally:
Docling parses the files, Ollama embeds them, Qdrant stores them.
"""
import hashlib
import os
import sys
import uuid
from io import BytesIO
from pathlib import Path

import ollama
from docling.chunking import HybridChunker
from docling.datamodel.base_models import DocumentStream, InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions, TesseractCliOcrOptions
from docling.document_converter import DocumentConverter, ImageFormatOption, PdfFormatOption
from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer
from qdrant_client import QdrantClient, models
from transformers import AutoTokenizer

QDRANT_URL = "http://127.0.0.1:6333"
COLLECTION = os.getenv("RAG_COLLECTION", "documents")
EMBED_MODEL = "bge-m3"
TOKENIZER = "BAAI/bge-m3"
MAX_CHUNK_TOKENS = 512
EMBED_BATCH = 32
TESSERACT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
TESSDATA = Path(__file__).resolve().parent / "tessdata"
OCR_LANGS = ["uzb", "uzb_cyrl", "rus", "eng"]
EXTENSIONS = {
    ".pdf", ".docx", ".pptx", ".xlsx", ".html", ".htm", ".md", ".csv", ".txt",
    ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp",
}


def embed(texts):
    vectors = []
    for i in range(0, len(texts), EMBED_BATCH):
        vectors += ollama.embed(model=EMBED_MODEL, input=texts[i : i + EMBED_BATCH]).embeddings
    return vectors


def source_filter(source, file_hash=None):
    conditions = [models.FieldCondition(key="source", match=models.MatchValue(value=source))]
    if file_hash:
        conditions.append(models.FieldCondition(key="file_hash", match=models.MatchValue(value=file_hash)))
    return models.Filter(must=conditions)


def is_indexed(client, source, file_hash):
    """True only if every chunk of this exact file version made it into Qdrant."""
    match = source_filter(source, file_hash)
    points, _ = client.scroll(COLLECTION, scroll_filter=match, limit=1, with_payload=["chunks_total"])
    if not points:
        return False
    return client.count(COLLECTION, count_filter=match, exact=True).count == points[0].payload["chunks_total"]


def convert(converter, path):
    if path.suffix.lower() == ".txt":  # Docling has no plain-text input; Markdown parses it fine
        stream = DocumentStream(name=f"{path.stem}.md", stream=BytesIO(path.read_bytes()))
        return converter.convert(stream).document
    return converter.convert(path).document


def build_points(document, chunker, source, file_name, file_hash):
    chunks = list(chunker.chunk(document))
    texts = [chunker.contextualize(chunk) for chunk in chunks]
    if not any(text.strip() for text in texts):
        raise ValueError("no text could be extracted")

    points = []
    for index, (chunk, text, vector) in enumerate(zip(chunks, texts, embed(texts))):
        pages = sorted({prov.page_no for item in chunk.meta.doc_items for prov in item.prov})
        points.append(models.PointStruct(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source}:{file_hash}:{index}")),
            vector=vector,
            payload={
                "source": source,
                "file_name": file_name,
                "file_hash": file_hash,
                "chunk_index": index,
                "chunks_total": len(chunks),
                "pages": pages,
                "headings": chunk.meta.headings or [],
                "text": text,
            },
        ))
    return points


def open_collection():
    client = QdrantClient(url=QDRANT_URL)
    if not client.collection_exists(COLLECTION):
        size = len(embed(["probe"])[0])
        client.create_collection(
            COLLECTION,
            vectors_config=models.VectorParams(size=size, distance=models.Distance.COSINE),
        )
    return client


def build_pipeline():
    os.environ["TESSDATA_PREFIX"] = str(TESSDATA)  # Docling's language check ignores the `path` option
    pipeline = PdfPipelineOptions(ocr_options=TesseractCliOcrOptions(lang=OCR_LANGS, tesseract_cmd=TESSERACT))
    converter = DocumentConverter(format_options={
        InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline),
        InputFormat.IMAGE: ImageFormatOption(pipeline_options=pipeline),
    })
    tokenizer = HuggingFaceTokenizer(
        tokenizer=AutoTokenizer.from_pretrained(TOKENIZER),
        max_tokens=MAX_CHUNK_TOKENS,
    )
    return converter, HybridChunker(tokenizer=tokenizer)


def is_supported(path):
    return path.suffix.lower() in EXTENSIONS and not path.name.startswith("~$")


def index_file(client, converter, chunker, path):
    """Return the number of chunks written, or None if this file version is already stored."""
    source = str(path)
    with path.open("rb") as f:
        file_hash = hashlib.file_digest(f, "sha256").hexdigest()
    if is_indexed(client, source, file_hash):
        return None
    points = build_points(convert(converter, path), chunker, source, path.name, file_hash)
    # Old chunks go only after the new ones are embedded, so a parse or embed failure keeps them.
    client.delete(COLLECTION, points_selector=models.FilterSelector(filter=source_filter(source)))
    client.upload_points(COLLECTION, points=points, batch_size=64, wait=True)
    return len(points)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) != 2:
        sys.exit("Usage: python ingest.py <folder>")
    folder = Path(sys.argv[1]).resolve()
    if not folder.is_dir():
        sys.exit(f"Not a folder: {folder}")

    files = [p for p in sorted(folder.rglob("*")) if p.is_file() and not p.name.startswith("~$")]
    supported = [p for p in files if is_supported(p)]
    unsupported = [p for p in files if not is_supported(p)]

    client = open_collection()
    converter, chunker = build_pipeline()

    indexed, unchanged, failed = 0, 0, []
    for path in supported:
        try:
            count = index_file(client, converter, chunker, path)
        except Exception as error:
            failed.append(path)
            print(f"FAILED    {path.name}: {error}")
            continue
        if count is None:
            unchanged += 1
            print(f"UNCHANGED {path.name}")
        else:
            indexed += 1
            print(f"INDEXED   {path.name}: {count} chunks")

    for path in unsupported:
        print(f"SKIPPED   {path.name}: unsupported format")
    print(f"\nindexed={indexed} unchanged={unchanged} failed={len(failed)} unsupported={len(unsupported)}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
