"""Retrieve the best chunks for a question and have the local LLM answer from them only."""
import os

import ollama
from qdrant_client import QdrantClient

QDRANT_URL = "http://127.0.0.1:6333"
COLLECTION = os.getenv("RAG_COLLECTION", "documents")
EMBED_MODEL = "bge-m3"
LLM_MODEL = os.getenv("RAG_LLM", "qwen2.5:7b")
TOP_K = 5
MIN_SCORE = 0.40  # below this a chunk is unrelated noise (measured: relevant >= 0.47, unrelated <= 0.39)
NOT_FOUND = "Hujjatlarda bu savolga javob topilmadi."

SYSTEM_PROMPT = f"""Siz hujjatlar bo'yicha yordamchisiz. Faqat quyidagi "Kontekst" ichidagi ma'lumotga tayanib javob bering.
Qoidalar:
- Savol qaysi tilda va yozuvda (lotin yoki kirill) berilgan bo'lsa, o'sha tilda va yozuvda javob bering.
- Ma'lumotni kontekstdan aynan ko'chiring: nom, raqam va sanani o'zgartirmang. O'zingizdan hech narsa qo'shmang.
- Javobdan keyin foydalangan manbaning raqamini [1], [2] shaklida yozing.
- Agar kontekstda javob bo'lmasa, faqat shu gapni yozing: {NOT_FOUND}"""


def retrieve(question, client=None):
    client = client or QdrantClient(url=QDRANT_URL)
    vector = ollama.embed(model=EMBED_MODEL, input=[question]).embeddings[0]
    hits = client.query_points(COLLECTION, query=vector, limit=TOP_K, with_payload=True).points
    return [h for h in hits if h.score >= MIN_SCORE]


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
    stream = ollama.chat(
        model=LLM_MODEL,
        messages=build_messages(question, hits),
        stream=True,
        options={"temperature": 0, "num_ctx": 8192},
    )
    for piece in stream:
        yield piece.message.content
