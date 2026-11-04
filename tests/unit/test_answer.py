from types import SimpleNamespace

import pytest

from rag import answer, config, llm
from tests.helpers import make_hit


class FakeSearchClient:
    def __init__(self, hits):
        self.hits = hits
        self.queries = []

    def query_points(self, collection, query, limit, with_payload):
        self.queries.append((collection, query, limit))
        return SimpleNamespace(points=self.hits)


def test_retrieve_drops_chunks_below_the_score_threshold(monkeypatch):
    monkeypatch.setattr(llm, "embed", lambda texts: [[0.1, 0.2]])
    client = FakeSearchClient([make_hit(0.9), make_hit(0.5), make_hit(0.39)])
    kept = answer.retrieve("savol", client)
    assert [h.score for h in kept] == [0.9, 0.5]
    assert client.queries == [(config.COLLECTION, [0.1, 0.2], config.TOP_K)]


def test_retrieve_handles_an_empty_index(monkeypatch):
    monkeypatch.setattr(llm, "embed", lambda texts: [[0.0]])
    assert answer.retrieve("savol", FakeSearchClient([])) == []


def test_no_chunks_means_not_found_without_calling_the_model(monkeypatch):
    def must_not_run(messages):
        raise AssertionError("the LLM must not be called without context")

    monkeypatch.setattr(llm, "chat_stream", must_not_run)
    assert list(answer.stream_answer("savol", [])) == [answer.NOT_FOUND]


def test_messages_number_the_sources_and_name_the_files():
    hits = [make_hit(file_name="a.pdf", text="birinchi"), make_hit(file_name="b.docx", text="ikkinchi")]
    system, user = answer.build_messages("Nima?", hits)
    assert system["role"] == "system" and answer.NOT_FOUND in system["content"]
    assert "[1] (fayl: a.pdf) birinchi" in user["content"]
    assert "[2] (fayl: b.docx) ikkinchi" in user["content"]
    assert user["content"].endswith("Savol: Nima?")


def test_stream_answer_passes_the_built_messages_to_the_model(monkeypatch):
    seen = []
    monkeypatch.setattr(llm, "chat_stream", lambda messages: seen.append(messages) or iter(["javob"]))
    hits = [make_hit()]
    assert list(answer.stream_answer("savol", hits)) == ["javob"]
    assert seen == [answer.build_messages("savol", hits)]


def test_cited_indices_are_ordered_and_deduplicated():
    assert answer.cited_indices("javob [2] va [1], yana [2]") == [2, 1]
    assert answer.cited_indices("manbasiz") == []


def test_only_the_cited_sources_are_shown():
    hits = [make_hit(text="bir"), make_hit(text="ikki"), make_hit(text="uch")]
    used = answer.used_sources("Javob [3] va [1].", hits)
    assert [(n, h.payload["text"]) for n, h in used] == [(1, "bir"), (3, "uch")]  # source numbers are kept


def test_invented_source_numbers_are_ignored():
    hits = [make_hit(text="bir"), make_hit(text="ikki")]
    assert [n for n, _ in answer.used_sources("Javob [7] va [2]", hits)] == [2]


def test_without_any_valid_citation_every_retrieved_source_is_shown():
    hits = [make_hit(text="bir"), make_hit(text="ikki")]
    assert [n for n, _ in answer.used_sources("Javob manbasiz", hits)] == [1, 2]
    assert [n for n, _ in answer.used_sources("Javob [9]", hits)] == [1, 2]


@pytest.mark.parametrize("value", [0.0, 1.0])
def test_threshold_is_configurable(monkeypatch, value):
    monkeypatch.setattr(config, "MIN_SCORE", value)
    monkeypatch.setattr(llm, "embed", lambda texts: [[0.0]])
    kept = answer.retrieve("q", FakeSearchClient([make_hit(0.5)]))
    assert len(kept) == (1 if value == 0.0 else 0)
