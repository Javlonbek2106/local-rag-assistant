"""Measure retrieval and answer quality on a set of documents and questions.

    python -m eval.run --set synthetic                 # generated sample documents (Windows, needs Arial)
    python -m eval.run --set eval/data/mine            # <dir>/docs/* and <dir>/questions.jsonl
    options: --no-answer (retrieval only, fast)  --limit N  --keep (do not delete the index afterwards)

questions.jsonl, one JSON object per line:
    {"q": "...", "file": "a.pdf", "facts": ["850 litr"], "page": 12}     answerable (page is optional)
    "file" may be a list of equivalent files (the same report in three languages); a fact may be a list of
    acceptable wordings: "facts": [["35", "thirty-five"], "years"]; numbers match with or without thousands separators
    {"q": "...", "answerable": false}                                      the documents cannot answer it
    add "forbidden": ["HACKED"] to an answerable question whose document holds an injected instruction:
    the answer must not contain those words. Ready-made set: eval/sets/injection
Real documents, questions and reports are git-ignored (eval/data, eval/reports).
"""
import argparse
import json
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

from eval import metrics
from rag import answer, config, ingest, llm, store

DEEP_K = 10  # retrieval depth measured, independent of the production TOP_K


def load_set(spec, workdir):
    """Return (docs_dir, questions)."""
    if spec == "synthetic":
        from tests.fixtures.make_samples import CASES, UNANSWERABLE, make_samples

        docs = make_samples(Path(workdir) / "docs")
        pages = {"03_server.pdf": 3}
        questions = [{"q": q, "file": f, "facts": [fact], **({"page": pages[f]} if f in pages else {})}
                     for q, f, fact in CASES]
        questions += [{"q": q, "answerable": False} for q in UNANSWERABLE]
        return docs, questions
    base = Path(spec)
    lines = (base / "questions.jsonl").read_text(encoding="utf-8").splitlines()
    return base / "docs", [json.loads(line) for line in lines if line.strip()]


def index_documents(client, docs):
    converter, chunker = ingest.build_pipeline()
    timings, failures = {}, {}
    for path in sorted(p for p in docs.iterdir() if p.is_file() and ingest.is_supported(p)):
        start = time.time()
        try:
            ingest.index_file(client, converter, chunker, path)
            timings[path.name] = round(time.time() - start, 1)
        except Exception as error:  # noqa: BLE001 - report every unreadable document, do not stop the run
            failures[path.name] = str(error)[:200]
    return timings, failures


def evaluate_question(client, case, with_answer):
    # the production search, only deeper; evaluation indexes have no versions
    hits = answer.search(case["q"], client, DEEP_K, answer.UNVERSIONED)
    files = [h.payload["file_name"] for h in hits]
    pages = [h.payload["pages"] for h in hits]
    texts = [h.payload["text"] for h in hits]
    answerable = case.get("answerable", True)
    top_score = round(max((h.score for h in hits), default=0.0), 3)
    record = {"q": case["q"], "answerable": answerable, "top_score": top_score, "top_files": files[:3]}
    if answerable:
        facts = case.get("facts", [])
        record["file_rank"] = metrics.file_rank(files, case["file"])
        record["fact_rank"] = metrics.fact_rank(texts, facts)
        if "page" in case and isinstance(case["file"], str):  # page numbers differ between equivalent files
            record["page_hit_5"] = metrics.page_hit(files, pages, case["file"], case["page"], 5)
    if with_answer:
        start = time.time()
        used = answer.above_threshold(hits[: config.TOP_K])
        text = "".join(answer.stream_answer(case["q"], used)).strip()
        record["seconds"] = round(time.time() - start, 1)
        record["answer"] = text
        if answerable:
            facts = case.get("facts", [])
            record["answer_ok"] = metrics.contains_facts(text, facts) and answer.NOT_FOUND not in text
            record["cited_ok"] = metrics.citations_grounded(text, [h.payload["text"] for h in used], facts)
            if case.get("forbidden"):  # the document holds an injected instruction; did the answer obey it?
                record["clean"] = metrics.contains_none(text, case["forbidden"])
        else:
            record["rejected"] = answer.NOT_FOUND in text
    return record


def pct(value):
    return "n/a" if value is None else f"{value * 100:.0f}%"


def summarize(records, with_answer):
    ans = [r for r in records if r["answerable"]]
    unans = [r for r in records if not r["answerable"]]
    file_ranks = [r["file_rank"] for r in ans]
    fact_ranks = [r["fact_rank"] for r in ans]
    s = {"questions": len(records), "answerable": len(ans), "unanswerable": len(unans)}
    s["file_recall"] = {k: metrics.recall_at(file_ranks, k) for k in (1, 3, 5)}
    s["fact_recall"] = {k: metrics.recall_at(fact_ranks, k) for k in (1, 3, 5)}
    s["file_mrr"] = metrics.mrr(file_ranks)
    pages = [r["page_hit_5"] for r in ans if "page_hit_5" in r]
    s["page_hit_5"] = sum(pages) / len(pages) if pages else None
    a_scores = [r["top_score"] for r in ans]
    u_scores = [r["top_score"] for r in unans]
    threshold, accuracy = metrics.best_threshold(a_scores, u_scores)
    s["threshold"] = {
        "current": config.MIN_SCORE,
        "answerable_let_through": sum(x >= config.MIN_SCORE for x in a_scores) / len(a_scores) if a_scores else None,
        "unanswerable_stopped": sum(x < config.MIN_SCORE for x in u_scores) / len(u_scores) if u_scores else None,
        "answerable_score_min_median": (min(a_scores), metrics.median(a_scores)) if a_scores else None,
        "unanswerable_score_median_max": (metrics.median(u_scores), max(u_scores)) if u_scores else None,
        "best": threshold, "best_balanced_accuracy": accuracy,
    }
    if with_answer:
        s["answer_ok"] = sum(r["answer_ok"] for r in ans) / len(ans) if ans else None
        s["cited_ok"] = sum(r["cited_ok"] for r in ans) / len(ans) if ans else None
        s["unanswerable_rejected"] = sum(r["rejected"] for r in unans) / len(unans) if unans else None
        injected = [r["clean"] for r in ans if "clean" in r]
        s["injection_resistance"] = sum(injected) / len(injected) if injected else None
        times = [r["seconds"] for r in records]
        s["latency_s"] = {"p50": metrics.percentile(times, 50), "p95": metrics.percentile(times, 95)}
    return s


def print_report(s, records, timings, failures, with_answer):
    print(f"\nquestions: {s['questions']} ({s['answerable']} answerable, {s['unanswerable']} not answerable)")
    print("\nRETRIEVAL (answerable questions)")
    for label, key in (("right file found", "file_recall"), ("right passage found", "fact_recall")):
        r = s[key]
        print(f"  {label:<20} @1 {pct(r[1])}  @3 {pct(r[3])}  @5 {pct(r[5])}")
    print(f"  file MRR {s['file_mrr']:.3f}   right page in top 5: {pct(s['page_hit_5'])}")
    t = s["threshold"]
    print(f"\nSCORE THRESHOLD (current {t['current']})")
    print(f"  answerable let through {pct(t['answerable_let_through'])}   "
          f"not answerable stopped {pct(t['unanswerable_stopped'])}")
    print(f"  top score, answerable (min, median): {t['answerable_score_min_median']}")
    print(f"  top score, not answerable (median, max): {t['unanswerable_score_median_max']}")
    print(f"  best cut-off on this set: {t['best']} (balanced accuracy {pct(t['best_balanced_accuracy'])})")
    if with_answer:
        print("\nANSWERS")
        print(f"  fact in answer {pct(s['answer_ok'])}   cited source really holds the fact {pct(s['cited_ok'])}")
        print(f"  not-answerable questions correctly refused {pct(s['unanswerable_rejected'])}")
        if s["injection_resistance"] is not None:
            print(f"  injected instructions in documents NOT obeyed: {pct(s['injection_resistance'])}")
        print(f"  latency p50 {s['latency_s']['p50']}s  p95 {s['latency_s']['p95']}s")
    if failures:
        print("\nDOCUMENTS THAT COULD NOT BE READ")
        for name, error in failures.items():
            print(f"  {name}: {error}")
    def needs_attention(r):
        if r["answerable"]:
            return r["file_rank"] != 1 or not r.get("answer_ok", True) or not r.get("clean", True)
        return r.get("rejected") is False

    bad = [r for r in records if needs_attention(r)]
    if bad:
        print("\nTO LOOK AT")
        for r in bad:
            why = "should be refused"
            if r["answerable"]:
                why = f"file rank {r.get('file_rank')}, passage rank {r.get('fact_rank')}"
            print(f"  - {r['q']}  [{why}; top files {r['top_files']}]")
            if "answer" in r:
                print(f"      answer: {r['answer'][:150]!r}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--set", required=True, dest="spec")
    parser.add_argument("--no-answer", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--only-files", help="comma-separated file name prefixes: ask only the questions about these "
                                             "files (questions the documents cannot answer are left out)")
    parser.add_argument("--keep", action="store_true", help="keep the index afterwards and print its name")
    parser.add_argument("--collection", help="use an index kept earlier with --keep (never deleted afterwards); "
                                             "documents that changed or are missing are indexed, the rest skipped")
    parser.add_argument("--no-index", action="store_true", help="with --collection: do not index at all")
    parser.add_argument("--workers", type=int, default=1, help="index with this many processes at once")
    args = parser.parse_args()
    with_answer = not args.no_answer

    with tempfile.TemporaryDirectory() as work:
        docs, questions = load_set(args.spec, work)
        if args.only_files:
            prefixes = tuple(args.only_files.split(","))
            def files_of(q):
                return [q["file"]] if isinstance(q["file"], str) else q["file"]

            questions = [q for q in questions
                         if q.get("answerable", True) and any(f.startswith(prefixes) for f in files_of(q))]
        if args.limit:
            questions = questions[: args.limit]
        reuse = bool(args.collection)
        config.COLLECTION = args.collection or f"rag_eval_{uuid.uuid4().hex[:8]}"
        client = store.get_client()
        if reuse and not client.collection_exists(config.COLLECTION):
            sys.exit(f"No such index: {config.COLLECTION}")
        store.ensure_collection(client, lambda: len(llm.embed(["probe"])[0]))
        try:
            timings, failures = {}, {}
            if args.no_index:
                print(f"using the existing index {config.COLLECTION} as it is")
            else:
                print(f"indexing {docs} ... (index name: {config.COLLECTION})", flush=True)
                if args.workers > 1:
                    from eval.index_pool import index_all

                    files = sorted((p for p in docs.iterdir() if p.is_file() and ingest.is_supported(p)),
                                   key=lambda p: -p.stat().st_size)  # biggest first: no long tail at the end
                    timings, failures = index_all(files, config.COLLECTION, args.workers)
                else:
                    timings, failures = index_documents(client, docs)
            records = []
            for i, case in enumerate(questions, 1):
                records.append(evaluate_question(client, case, with_answer))
                print(f"  {i}/{len(questions)}", end="\r", flush=True)
            summary = summarize(records, with_answer)
            print_report(summary, records, timings, failures, with_answer)
            out = Path(__file__).parent / "reports"
            out.mkdir(exist_ok=True)
            path = out / f"{datetime.now():%Y%m%d-%H%M%S}-{Path(args.spec).name}.json"
            names = ("EMBED_MODEL", "LLM_MODEL", "TOP_K", "MIN_SCORE", "MAX_CHUNK_TOKENS", "RERANK_MODEL")
            settings = {k: getattr(config, k) for k in names}
            path.write_text(json.dumps({"settings": settings, "summary": summary, "index_seconds": timings,
                                        "failed_documents": failures, "questions": records},
                                       ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\nreport: {path}")
        finally:
            if args.keep or reuse:
                print(f"index kept: {config.COLLECTION}")
            else:
                client.delete_collection(config.COLLECTION)


if __name__ == "__main__":
    main()
