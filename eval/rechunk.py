"""Build an index from the parsed-document cache with a chunking variant. Nothing is parsed again.

    python -m eval.rechunk --docs eval/data/meniki/docs --collection NAME [--max-tokens 512] [--tables markdown]
                           [--only 05,11]       (only files whose name starts with one of these prefixes)

Then measure it without indexing again:
    python -m eval.run --set eval/data/meniki --collection NAME --no-index [--no-answer]
"""
import argparse
import hashlib
import sys
import time
from pathlib import Path

from eval.parse_cache import cache_path
from rag import config, ingest, llm, store


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--docs", required=True)
    parser.add_argument("--collection", required=True)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--tables", choices=("triplet", "markdown"))
    parser.add_argument("--only", help="comma-separated file name prefixes")
    args = parser.parse_args()

    from docling_core.types.doc import DoclingDocument

    config.COLLECTION = args.collection
    client = store.get_client()
    store.ensure_collection(client, lambda: len(llm.embed(["probe"])[0]))
    chunker = ingest.build_chunker(max_tokens=args.max_tokens, tables=args.tables)
    prefixes = tuple(args.only.split(",")) if args.only else ()
    docs = Path(args.docs)
    total = 0
    start = time.time()
    for path in sorted(p for p in docs.iterdir() if p.is_file() and ingest.is_supported(p)):
        if prefixes and not path.name.startswith(prefixes):
            continue
        began = time.time()
        file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        # the same source string the evaluation uses, so one index can be topped up by either tool
        if path.suffix.lower() == ".xlsx":  # spreadsheets do not go through Docling at all
            points = ingest.xlsx_points(path, str(path), file_hash)
        else:
            cached = cache_path(docs, path.name)
            if not cached.exists():
                print(f"  no parsed copy of {path.name}, skipped", flush=True)
                continue
            points = ingest.build_points(DoclingDocument.load_from_json(cached), chunker, str(path), path.name,
                                         file_hash)
        store.delete_source(client, str(path))
        client.upload_points(config.COLLECTION, points=points, batch_size=64, wait=True)
        total += len(points)
        print(f"  {path.name}: {len(points)} chunks in {time.time() - began:.0f}s", flush=True)
    print(f"{total} chunks in {args.collection} after {(time.time() - start) / 60:.1f} min "
          f"(max_tokens={args.max_tokens or config.MAX_CHUNK_TOKENS}, tables={args.tables or config.TABLE_FORMAT})")


if __name__ == "__main__":
    main()
