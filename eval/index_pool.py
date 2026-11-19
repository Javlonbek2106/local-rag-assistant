"""Index documents in several processes at once.

Docling handles one document at a time and uses only a few CPU cores, so a long list of big documents is slow.
Separate processes give a near-linear speed-up (up to the number of free cores). Unchanged documents are skipped by
hash, so this can also top up an index another run is still building.

    python -m eval.index_pool --docs eval/data/meniki/docs --collection NAME --workers 3 [--reverse]
"""
import argparse
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

_state = {}


def _init(collection, threads):
    import torch

    from rag import config, ingest, store

    torch.set_num_threads(threads)  # without this every process would try to use all cores
    config.COLLECTION = collection
    _state["client"] = store.get_client()
    _state["converter"], _state["chunker"] = ingest.build_pipeline()


def _index_one(path_text):
    from rag import ingest

    start = time.time()
    try:
        count = ingest.index_file(_state["client"], _state["converter"], _state["chunker"], Path(path_text))
        return path_text, round(time.time() - start, 1), None, count
    except Exception as error:  # noqa: BLE001 - report every unreadable document, do not stop the others
        return path_text, round(time.time() - start, 1), str(error)[:200], None


def index_all(paths, collection, workers, threads=None):
    """Index `paths` with `workers` processes. Returns ({name: seconds}, {name: error})."""
    threads = threads or max(2, (os.cpu_count() or 8) // workers)
    timings, failures = {}, {}
    with ProcessPoolExecutor(max_workers=workers, initializer=_init, initargs=(collection, threads)) as pool:
        futures = [pool.submit(_index_one, str(p)) for p in paths]
        for future in as_completed(futures):
            path_text, seconds, error, count = future.result()
            name = Path(path_text).name
            if error:
                failures[name] = error
                print(f"  FAILED   {name} after {seconds}s: {error}", flush=True)
            else:
                timings[name] = seconds
                state = "unchanged" if count is None else f"{count} chunks"
                print(f"  done     {name}: {state} in {seconds}s", flush=True)
    return timings, failures


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--docs", required=True)
    parser.add_argument("--collection", required=True)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--reverse", action="store_true", help="start from the last file name (to meet another run)")
    args = parser.parse_args()

    from rag import ingest

    docs = Path(args.docs)
    paths = sorted((p for p in docs.iterdir() if p.is_file() and ingest.is_supported(p)), reverse=args.reverse)
    print(f"indexing {len(paths)} documents into {args.collection} with {args.workers} processes", flush=True)
    start = time.time()
    timings, failures = index_all(paths, args.collection, args.workers)
    print(f"finished in {(time.time() - start) / 60:.1f} min; failed: {failures or 'none'}")


if __name__ == "__main__":
    main()
