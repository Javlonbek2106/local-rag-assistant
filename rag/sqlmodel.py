"""The language model for SQL generation (roadmap M4.3): the same local Ollama as the answers, through rag/llm.py, with
its own model name, window and output limit (RAG_SQL_MODEL, RAG_SQL_NUM_CTX, RAG_SQL_NUM_PREDICT), asking for JSON.

This is the only place that knows both the connector's generator and the model client: the connector package does not
import rag.llm (tests/unit/test_connector_boundaries.py). It is handed to the connector service as a plain function.
`complete(messages)` runs in the thread that serves the request; when that thread has a cancellation scope bound (the
web layer binds one and cancels it if the client goes away) the call is cancellable, and it keeps that scope.
"""

from rag import config, llm
from rag.connector.errors import ModelError
from rag.connector.sqlgen import Completion


def complete(messages):
    """Ask the model and return Completion(text, model, truncated). Raises ModelError with a fixed code (llm_timeout,
    llm_unavailable, llm_error, llm_cancelled); the text of Ollama's own error is dropped on purpose: it can name paths
    and quote the prompt."""
    scope = llm.current_scope() or llm.Scope()
    scope.truncated = False
    pieces = []
    try:
        with llm.bind(scope):
            for piece in llm.chat_stream(
                messages,
                model=config.SQL_MODEL,
                num_ctx=config.SQL_NUM_CTX,
                num_predict=config.SQL_NUM_PREDICT,
                response_format="json",
            ):
                pieces.append(piece)
    except llm.LLMTimeout:
        raise ModelError("llm_timeout") from None
    except llm.LLMUnavailable:
        raise ModelError("llm_unavailable") from None
    except llm.LLMCancelled:
        raise ModelError("llm_cancelled") from None
    except llm.LLMError:
        raise ModelError("llm_error") from None
    return Completion("".join(pieces), config.SQL_MODEL, bool(scope.truncated))
