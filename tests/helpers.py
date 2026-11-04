from types import SimpleNamespace


def make_hit(score=0.7, file_name="a.txt", text="fakt", pages=None):
    """A search hit shaped like Qdrant's ScoredPoint, as far as the code under test reads it."""
    return SimpleNamespace(score=score, payload={"file_name": file_name, "text": text, "pages": pages or []})
