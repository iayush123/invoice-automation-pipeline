"""
LLM client factory.

Why this abstraction exists:
──────────────────────────────
We support both Anthropic (claude-opus-4-8, vision-capable) and OpenAI (gpt-4o).
Centralising model selection here means the rest of the codebase never
imports `anthropic` or `openai` directly — only this module does.
If a new model ships, one line changes here, nothing else breaks.

Model selection rationale (as of mid-2025):
  - Anthropic claude-opus-4-8: latest Anthropic vision model, strong on
    structured document extraction, good at following JSON schemas.
  - OpenAI gpt-4o: strong alternative with vision; use if Anthropic quota issues.

Always check https://docs.anthropic.com/en/docs/about-claude/models for the
current model string before going to production.
"""

from app.core.config import settings


def get_llm_client():
    """Return a configured LLM client for the provider specified in settings."""
    if settings.llm_provider == "anthropic":
        import anthropic
        if not settings.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY not set")
        return anthropic.Anthropic(api_key=settings.anthropic_api_key)

    elif settings.llm_provider == "openai":
        from openai import OpenAI
        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY not set")
        return OpenAI(api_key=settings.openai_api_key)

    else:
        raise ValueError(f"Unknown LLM_PROVIDER: {settings.llm_provider!r}")


def get_langchain_llm():
    """
    Return a LangChain-compatible chat model for use inside LangGraph nodes.
    We keep LangChain's surface thin — only the graph nodes use this.
    """
    if settings.llm_provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=settings.llm_model,
            anthropic_api_key=settings.anthropic_api_key,
            max_tokens=4096,
        )
    elif settings.llm_provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=settings.llm_model,
            openai_api_key=settings.openai_api_key,
            max_tokens=4096,
        )
    else:
        raise ValueError(f"Unknown LLM_PROVIDER: {settings.llm_provider!r}")
