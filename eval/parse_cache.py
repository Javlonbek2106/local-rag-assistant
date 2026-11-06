"""Parse every document once and keep Docling's result on disk.

Parsing (layout, tables, OCR) is by far the slowest step and does not depend on how the text is chunked. With the
parsed documents cached, a chunking experiment only re-chunks and re-embeds (minutes) instead of re-parsing (an hour).

    python -m eval.parse_cache --docs eval/data/meniki/docs --workers 4
    (writes eval/data/meniki/.parsed/<file name>.json; finished files are skipped on a re-run)
"""
import argparse
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

_state = {}


def cache_path(docs, name):
    return Path(docs).parent / ".parsed" / f"{name}.json"


def _init(threads):
    import torch

    from rag import ingest

    torch.set_num_threads(threads)
    _state["converter"], _ = ingest.build_pipeline()


def _parse_one(path_text):
    from rag import ingest

    path = Path(path_text)
    target = cache_path(path.parent, path.name)
    if target.exists():
        return path.name, 0.0, None, True
    start = time.time()
    try:
        document = ingest.convert(_state["converter"], path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        document.save_as_json(tmp)
        os.replace(tmp, target)  # a half-written cache file must never be mistaken for a finished one
        return path.name, round(time.time() - start, 1), None, False
    except Exception as error:  # noqa: BLE001 - report every unreadable document, do not stop the others
        return path.name, round(time.time() - start, 1), str(error)[:200], False


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--docs", required=True)
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()

    from rag import ingest

    docs = Path(args.docs)
    files = sorted((p for p in docs.iterdir() if p.is_file() and ingest.is_supported(p)),
                   key=lambda p: -p.stat().st_size)  # biggest first: no long tail at the end
    threads = max(2, (os.cpu_count() or 8) // args.workers)
    print(f"parsing {len(files)} documents with {args.workers} processes ({threads} threads each)", flush=True)
    start = time.time()
    failed = {}
    with ProcessPoolExecutor(max_workers=args.workers, initializer=_init, initargs=(threads,)) as pool:
        for future in as_completed([pool.submit(_parse_one, str(p)) for p in files]):
            name, seconds, error, skipped = future.result()
            if error:
                failed[name] = error
                print(f"  FAILED   {name} after {seconds}s: {error}", flush=True)
            else:
                print(f"  {'cached' if skipped else 'parsed'}   {name} in {seconds}s", flush=True)
    print(f"finished in {(time.time() - start) / 60:.1f} min; failed: {failed or 'none'}")


if __name__ == "__main__":
    main()
