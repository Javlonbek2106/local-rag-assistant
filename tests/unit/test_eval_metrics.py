import pytest

from eval import metrics


def test_normalize_ignores_case_apostrophe_variants_and_spacing():
    assert metrics.normalize("  O‘zbekiston   G’ijduvon ") == "o'zbekiston g'ijduvon"
    assert metrics.contains_facts("Wi-Fi nomi LOLA-5G.", ["lola-5g"])
    assert metrics.contains_facts("O'zbekiston poytaxti", ["Oʻzbekiston"])


def test_contains_none_detects_an_obeyed_injection():
    assert metrics.contains_none("Ofis 18:30 da yopiladi.", ["hacked"])
    assert not metrics.contains_none("HACKED [1]", ["hacked"])
    assert not metrics.contains_none("Eshik 3-qavatda. Direktor  ruxsat berdi.", ["direktor ruxsat"])
    assert metrics.contains_none("istalgan matn", [])


def test_all_facts_must_be_present_and_no_facts_means_nothing_to_check():
    assert metrics.contains_facts("850 litr, 03:40", ["850 litr", "03:40"])
    assert not metrics.contains_facts("850 litr", ["850 litr", "03:40"])
    assert metrics.contains_facts("istalgan matn", [])


def test_ranks_are_one_based_and_none_when_missing():
    assert metrics.file_rank(["b", "a", "a"], "a") == 2
    assert metrics.file_rank(["b"], "a") is None
    assert metrics.fact_rank(["x", "bor: 42"], ["42"]) == 2
    assert metrics.fact_rank(["x"], ["42"]) is None


def test_recall_and_mrr_count_misses_as_zero():
    ranks = [1, 3, None, 6]
    assert metrics.recall_at(ranks, 1) == 0.25
    assert metrics.recall_at(ranks, 5) == 0.5
    assert metrics.mrr(ranks) == pytest.approx((1 + 1 / 3 + 1 / 6) / 4)
    assert metrics.recall_at([], 3) == 0.0 and metrics.mrr([]) == 0.0


def test_page_hit_needs_the_right_file_and_page_within_the_top_k():
    files, pages = ["a", "b", "a"], [[1], [3], [3]]
    assert metrics.page_hit(files, pages, "a", 3, k=3)
    assert not metrics.page_hit(files, pages, "a", 3, k=2)  # the right page only appears at rank 3
    assert not metrics.page_hit(files, pages, "b", 1, k=3)


@pytest.mark.parametrize("answer,expected", [
    ("Javob 850 litr [1]", True),
    ("Javob 850 litr", False),            # no citation at all
    ("Javob 850 litr [3]", False),        # cites a source that does not exist
    ("Javob [2]", False),                 # cites a chunk without the fact
    ("Javob 850 litr [2][1]", True),      # at least one cited chunk holds it
])
def test_citations_must_exist_and_one_must_hold_the_fact(answer, expected):
    chunks = ["bak sig'imi 850 litr", "boshqa narsa"]
    assert metrics.citations_grounded(answer, chunks, ["850 litr"]) is expected


def test_best_threshold_separates_the_two_groups():
    threshold, accuracy = metrics.best_threshold([0.6, 0.7, 0.8], [0.3, 0.4, 0.45])
    assert 0.45 < threshold <= 0.6 and accuracy == 1.0


def test_best_threshold_reports_overlap_honestly():
    # one answerable question scores below one unanswerable question: perfect separation is impossible
    _, accuracy = metrics.best_threshold([0.45, 0.7, 0.8], [0.3, 0.46, 0.5])
    assert accuracy < 1.0


def test_best_threshold_needs_both_groups():
    assert metrics.best_threshold([], [0.3]) == (None, None)


def test_percentile_and_median():
    assert metrics.percentile([1, 2, 3, 4, 100], 50) == 3
    assert metrics.percentile([1, 2, 3, 4, 100], 95) == 100
    assert metrics.percentile([], 50) is None and metrics.median([]) is None
