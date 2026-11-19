"""Re-order the retrieved chunks with a cross-encoder that reads the question and each chunk together.

Vector search finds the right chunks but often not at the top; the cross-encoder fixes the order.
The model is loaded on first use (a few seconds) and kept for the life of the process.
"""
import threading

from rag import config

_lock = threading.Lock()
_loaded = None
BATCH = 8


def _model():
    global _loaded
    with _lock:
        if _loaded is None:
            from transformers import AutoModelForSequenceClassification, AutoTokenizer

            _loaded = (AutoTokenizer.from_pretrained(config.RERANK_MODEL),
                       AutoModelForSequenceClassification.from_pretrained(config.RERANK_MODEL).eval())
        return _loaded


def scores(question, texts):
    """Relevance score of every text to the question (higher is better)."""
    import torch

    tokenizer, model = _model()
    out = []
    with torch.no_grad():
        for i in range(0, len(texts), BATCH):
            part = texts[i:i + BATCH]
            batch = tokenizer([question] * len(part), part, padding=True, truncation=True, max_length=512,
                              return_tensors="pt")
            out += model(**batch).logits.view(-1).tolist()
    return out


def rerank(question, hits):
    """The same hits, best first."""
    if len(hits) < 2:
        return list(hits)
    relevance = scores(question, [h.payload["text"] for h in hits])
    order = sorted(range(len(hits)), key=lambda i: -relevance[i])
    return [hits[i] for i in order]
