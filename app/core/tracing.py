"""
LangSmith tracing setup.

Call configure_tracing() once at app startup. After that, every LangGraph
node execution is automatically traced — no extra code needed in the nodes.

Why LangSmith over raw logging?
─────────────────────────────────
Structured logs tell you what happened. LangSmith traces tell you why
a specific LLM call produced a specific output — you can see the exact
prompt, the raw model response, token counts, and latency for every step
of every invoice. That's the difference between debugging blind and
debugging with full context.

Traces land at: https://smith.langchain.com/projects/<LANGCHAIN_PROJECT>
"""

import os
import structlog

log = structlog.get_logger()


def configure_tracing() -> None:
    """
    Enable LangSmith tracing by setting the required environment variables.
    Called once from app startup; no-op if LANGCHAIN_API_KEY is not set.
    """
    from app.core.config import settings

    if not settings.langchain_api_key:
        log.info("langsmith_tracing_disabled", reason="LANGCHAIN_API_KEY not set")
        return

    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGCHAIN_API_KEY"] = settings.langchain_api_key
    os.environ["LANGCHAIN_PROJECT"] = settings.langchain_project

    log.info("langsmith_tracing_enabled", project=settings.langchain_project)


def trace_metadata(invoice_id: str, step: str) -> dict:
    """
    Return LangSmith run metadata tags to attach to a traced call.
    Pass as `metadata=trace_metadata(...)` to LangChain runnables.
    """
    return {
        "invoice_id": invoice_id,
        "pipeline_step": step,
    }
