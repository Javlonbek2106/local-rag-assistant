"""Thin wrappers around the local Ollama server."""
import ollama

from rag import config


def embed(texts):
    vectors = []
    for i in range(0, len(texts), config.EMBED_BATCH):
        vectors += ollama.embed(model=config.EMBED_MODEL, input=texts[i : i + config.EMBED_BATCH]).embeddings
    return vectors


def chat_stream(messages):
    """Yield the answer text piece by piece."""
    stream = ollama.chat(
        model=config.LLM_MODEL,
        messages=messages,
        stream=True,
        options={"temperature": 0, "num_ctx": config.LLM_NUM_CTX},
    )
    for piece in stream:
        yield piece.message.content
