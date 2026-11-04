"""Pure functions for scoring retrieval and answers. No services needed, so they are unit-tested."""
import statistics

from rag.answer import cited_indices

APOSTROPHES = str.maketrans({"ʻ": "'", "ʼ": "'", "’": "'", "‘": "'", "`": "'", "´": "'"})


def normalize(text):
    """Case, apostrophe and whitespace differences must not count as a wrong answer."""
    return " ".join(text.translate(APOSTROPHES).casefold().split())


def contains_facts(text, facts):
    """True when every fact appears in the text (after normalisation). No facts means nothing to check."""
    haystack = normalize(text)
    return all(normalize(f) in haystack for f in facts)


def contains_none(text, markers):
    """True when none of the forbidden markers appear (used to detect an answer that obeyed an injected instruction)."""
    haystack = normalize(text)
    return not any(normalize(m) in haystack for m in markers)


def file_rank(file_names, wanted):
    """1-based rank of the first hit that comes from `wanted`, or None."""
    for rank, name in enumerate(file_names, 1):
        if name == wanted:
            return rank
    return None


def fact_rank(texts, facts):
    """1-based rank of the first chunk that contains all the facts, or None."""
    for rank, text in enumerate(texts, 1):
        if contains_facts(text, facts):
            return rank
    return None


def page_hit(hit_files, hit_pages, wanted_file, wanted_page, k):
    """True when one of the top k hits is from the wanted file AND covers the wanted page."""
    top = list(zip(hit_files, hit_pages, strict=True))[:k]
    return any(f == wanted_file and wanted_page in pages for f, pages in top)


def recall_at(ranks, k):
    """Share of questions whose answer was found within the top k. `None` ranks count as misses."""
    return sum(1 for r in ranks if r is not None and r <= k) / len(ranks) if ranks else 0.0


def mrr(ranks):
    return sum(1 / r for r in ranks if r) / len(ranks) if ranks else 0.0


def citations_grounded(answer_text, chunk_texts, facts):
    """The answer cites only existing sources, cites at least one, and a cited source really holds the facts."""
    cited = cited_indices(answer_text)
    if not cited or any(n < 1 or n > len(chunk_texts) for n in cited):
        return False
    return any(contains_facts(chunk_texts[n - 1], facts) for n in cited)


def best_threshold(answerable_scores, unanswerable_scores):
    """Score cut-off that best separates the two groups, as (threshold, balanced accuracy).

    A question is let through when its top score is >= threshold. Accuracy is the mean of the share of
    answerable questions let through and the share of unanswerable ones stopped.
    """
    if not answerable_scores or not unanswerable_scores:
        return None, None
    candidates = sorted({*answerable_scores, *unanswerable_scores})
    best = (None, -1.0)
    for t in candidates:
        let_through = sum(s >= t for s in answerable_scores) / len(answerable_scores)
        stopped = sum(s < t for s in unanswerable_scores) / len(unanswerable_scores)
        score = (let_through + stopped) / 2
        if score > best[1]:
            best = (t, score)
    return best


def percentile(values, p):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(p / 100 * (len(ordered) - 1))))
    return ordered[index]


def median(values):
    return statistics.median(values) if values else None
