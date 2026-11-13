"""Parse documents, chunk them and store the chunks in Qdrant.

Command line: python -m rag.ingest <folder>
Puts every supported file of the folder into the upload queue, exactly like an upload in the web page; the
worker (started by `python -m rag`) does the indexing. Re-queued files that did not change are skipped by hash.
"""
import hashlib
import logging
import os
import shutil
import sys
import uuid
from io import BytesIO
from pathlib import Path

from rag import config, db, llm, storage, store

log = logging.getLogger(__name__)


class UnreadableDocument(ValueError):
    """The file cannot be turned into text. Retrying will not help."""


def is_supported(path):
    return path.suffix.lower() in config.EXTENSIONS and not path.name.startswith("~$")


def build_pipeline():
    """Load the heavy parsing models once. Imported here so the rest of the package stays light."""
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, TesseractCliOcrOptions
    from docling.document_converter import DocumentConverter, ImageFormatOption, PdfFormatOption

    os.environ["TESSDATA_PREFIX"] = str(config.TESSDATA)  # Docling's language check ignores the `path` option
    pipeline = PdfPipelineOptions(
        ocr_options=TesseractCliOcrOptions(lang=config.OCR_LANGS, tesseract_cmd=config.TESSERACT)
    )
    converter = DocumentConverter(format_options={
        InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline),
        InputFormat.IMAGE: ImageFormatOption(pipeline_options=pipeline),
    })
    return converter, build_chunker()


def build_chunker(max_tokens=None, tables=None):
    """The chunker alone (cheap compared to parsing). `tables` is "triplet" (one "row, column = value" sentence per
    cell, Docling's default) or "markdown" (real table rows; a long table is split between rows and the header row
    is repeated in every chunk). Defaults come from the configuration."""
    from docling.chunking import HybridChunker
    from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer
    from transformers import AutoTokenizer

    tokenizer = HuggingFaceTokenizer(
        tokenizer=AutoTokenizer.from_pretrained(config.TOKENIZER),
        max_tokens=max_tokens or config.MAX_CHUNK_TOKENS,
    )
    if (tables or config.TABLE_FORMAT) == "markdown":
        return HybridChunker(tokenizer=tokenizer, serializer_provider=_markdown_tables_provider())
    return HybridChunker(tokenizer=tokenizer)


def _markdown_tables_provider():
    from docling_core.transforms.chunker.hierarchical_chunker import ChunkingDocSerializer, ChunkingSerializerProvider
    from docling_core.transforms.serializer.markdown import MarkdownTableSerializer

    class MarkdownTablesProvider(ChunkingSerializerProvider):
        def get_serializer(self, doc):
            return ChunkingDocSerializer(doc=doc, table_serializer=MarkdownTableSerializer())

    return MarkdownTablesProvider()


def convert(converter, path):
    if path.suffix.lower() == ".txt":  # Docling has no plain-text input; Markdown parses it fine
        from docling.datamodel.base_models import DocumentStream

        stream = DocumentStream(name=f"{path.stem}.md", stream=BytesIO(path.read_bytes()))
        return converter.convert(stream).document
    return converter.convert(path).document


def _require_text(items):
    if not any(text.strip() for text, _, _ in items):
        raise UnreadableDocument("no text could be extracted")


def _points(items, vectors, source, file_name, file_hash, document_id=None, version_id=None):
    """Qdrant points for the chunks. A versioned chunk's id comes from its version and position, so writing the same
    version again overwrites the same points instead of adding duplicates."""
    from qdrant_client import models

    extra = {"document_id": str(document_id), "version_id": str(version_id)} if version_id else {}
    return [
        models.PointStruct(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL,
                              f"{version_id}:{index}" if version_id else f"{source}:{file_hash}:{index}")),
            vector=vector,
            payload={
                "source": source,
                "file_name": file_name,
                "file_hash": file_hash,
                "chunk_index": index,
                "chunks_total": len(items),
                "pages": pages,
                "headings": headings,
                "text": text,
                **extra,
            },
        )
        for index, ((text, pages, headings), vector) in enumerate(zip(items, vectors, strict=True))
    ]


def _make_points(items, source, file_name, file_hash):
    """`items` is one (text, pages, headings) per chunk. Embeds the texts and builds the Qdrant points."""
    _require_text(items)
    return _points(items, llm.embed([text for text, _, _ in items]), source, file_name, file_hash)


def document_items(document, chunker):
    chunks = list(chunker.chunk(document))
    return [
        (chunker.contextualize(chunk),
         sorted({prov.page_no for item in chunk.meta.doc_items for prov in item.prov}),
         chunk.meta.headings or [])
        for chunk in chunks
    ]


def build_points(document, chunker, source, file_name, file_hash):
    return _make_points(document_items(document, chunker), source, file_name, file_hash)


def xlsx_points(path, source, file_hash):
    """Spreadsheets bypass Docling: it takes a sheet's title row for the column header (see rag/xlsx.py)."""
    from rag import xlsx

    return _make_points(xlsx.chunks(path), source, path.name, file_hash)


def extract_items(converter, chunker, path):
    """Parse a file and cut it into (text, pages, headings) chunks."""
    if path.suffix.lower() == ".xlsx":
        from rag import xlsx

        return xlsx.chunks(path)
    return document_items(convert(converter, path), chunker)


def index_version(client, converter, chunker, path, *, document_id, version_id, file_name, file_hash,
                  stage=lambda status: None):
    """Index one document version into its own set of Qdrant points and return the number of chunks written.

    `stage` is told EMBEDDING and INDEXING as the work reaches them (the worker records them, and raises to stop if
    its claim was lost). The version's chunks from an earlier attempt are removed first, so a retry never leaves
    leftovers; no other version is touched. Verifying and activating are the caller's job.
    """
    items = extract_items(converter, chunker, path)
    _require_text(items)
    stage("EMBEDDING")
    vectors = llm.embed([text for text, _, _ in items])
    points = _points(items, vectors, str(document_id), file_name, file_hash, document_id, version_id)
    stage("INDEXING")
    store.delete_version(client, version_id)
    client.upload_points(config.COLLECTION, points=points, batch_size=64, wait=True)
    return len(points)


def index_file(client, converter, chunker, path, source=None):
    """Evaluation and command-line use, without a database: return the number of chunks written, or None if this
    exact file is already stored. `source` identifies the document in the index (defaults to the path)."""
    source = source or str(path)
    with path.open("rb") as f:
        file_hash = hashlib.file_digest(f, "sha256").hexdigest()
    if store.is_indexed(client, source, file_hash):
        return None
    points = _make_points(extract_items(converter, chunker, path), source, path.name, file_hash)
    # Old chunks go only after the new ones are embedded, so a parse or embed failure keeps them.
    store.delete_source(client, source)
    client.upload_points(config.COLLECTION, points=points, batch_size=64, wait=True)
    return len(points)


def queue_file(name, temp_file, size, repo=db):
    """Register a fully written temp file as the next version of the document `name` and queue it for indexing.

    Returns the version (see db.create_version). A file identical to the newest version is not queued again
    ("created" is False) and the temp file is discarded.
    """
    with temp_file.open("rb") as f:
        file_hash = hashlib.file_digest(f, "sha256").hexdigest()
    version = repo.create_version(name, size, file_hash)
    if not version["created"]:
        temp_file.unlink(missing_ok=True)
        return version
    storage.save(version["file_path"], temp_file)
    repo.enqueue(version["id"])
    return version


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING)
    if len(argv) != 2:
        sys.exit("Usage: python -m rag.ingest <folder>")
    folder = Path(argv[1]).resolve()
    if not folder.is_dir():
        sys.exit(f"Not a folder: {folder}")

    db.migrate()
    storage.temp_dir().mkdir(parents=True, exist_ok=True)
    queued, problems, seen = 0, [], set()
    for src in sorted(p for p in folder.rglob("*") if p.is_file() and not p.name.startswith("~$")):
        if not is_supported(src):  # Office lock files (~$...) are skipped silently above
            problems.append((src, "unsupported format"))
        elif src.name in seen:
            problems.append((src, "another file with the same name was already queued in this run"))
        else:
            seen.add(src.name)
            temp = storage.temp_dir() / uuid.uuid4().hex
            shutil.copyfile(src, temp)
            if queue_file(src.name, temp, src.stat().st_size)["created"]:
                queued += 1
                print(f"QUEUED    {src.name}")
            else:
                problems.append((src, "unchanged, already up to date"))
    for src, reason in problems:
        print(f"SKIPPED   {src.name}: {reason}")
    print(f"\nqueued={queued} skipped={len(problems)}")
    if queued:
        print("Indexing happens in the worker (it runs with `python -m rag`); watch progress in the web page.")
    sys.exit(0)


if __name__ == "__main__":
    main(sys.argv)
