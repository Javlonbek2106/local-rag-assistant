"""Retrieve the best chunks for a question and have the local LLM answer from them only."""
import logging
import re

from rag import config, llm, store

log = logging.getLogger(__name__)
CITATION = re.compile(r"\[(\d+)\]")
NOT_FOUND = "Hujjatlarda bu savolga javob topilmadi."

SYSTEM_PROMPT = f"""Siz hujjatlar bo'yicha yordamchisiz. Faqat quyidagi "Kontekst" ichidagi ma'lumotga tayanib javob bering.
Qoidalar:
- Savol qaysi tilda va yozuvda (lotin yoki kirill) berilgan bo'lsa, o'sha tilda va yozuvda javob bering.
- Ma'lumotni kontekstdan aynan ko'chiring: nom, raqam va sanani o'zgartirmang. O'zingizdan hech narsa qo'shmang.
- Javobdan keyin foydalangan manbaning raqamini [1], [2] shaklida yozing.
- Agar kontekstda javob bo'lmasa, faqat shu gapni yozing: {NOT_FOUND}"""


def cited_indices(answer_text):
    """The [n] source markers the model wrote, in order of first appearance, without duplicates."""
    seen = []
    for match in CITATION.finditer(answer_text):
        n = int(match.group(1))
        if n not in seen:
            seen.append(n)
    return seen


def used_sources(answer_text, hits):
    """(number, hit) pairs for the sources the answer really cites.

    Numbers the model invented (outside the list) are ignored. If it cited nothing valid, we cannot tell
    which source it used, so every retrieved source is returned rather than hiding the evidence.
    """
    numbered = list(enumerate(hits, 1))
    cited = [n for n in cited_indices(answer_text) if 1 <= n <= len(hits)]
    return [(n, h) for n, h in numbered if n in cited] or numbered


def retrieve(question, client=None):
    client = client or store.get_client()
    vector = llm.embed([question])[0]
    hits = client.query_points(config.COLLECTION, query=vector, limit=config.TOP_K, with_payload=True).points
    kept = [h for h in hits if h.score >= config.MIN_SCORE]
    log.info("retrieve: %d/%d chunks above %.2f, top score %.3f",
             len(kept), len(hits), config.MIN_SCORE, hits[0].score if hits else 0.0)
    return kept


def build_messages(question, hits):
    context = "\n\n".join(
        f"[{i}] (fayl: {h.payload['file_name']}) {h.payload['text']}" for i, h in enumerate(hits, 1)
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Kontekst:\n{context}\n\nSavol: {question}"},
    ]


def stream_answer(question, hits):
    """Yield answer text pieces. With no relevant chunks the LLM is not called at all."""
    if not hits:
        yield NOT_FOUND
        return
    yield from llm.chat_stream(build_messages(question, hits))
