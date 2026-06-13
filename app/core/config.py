"""
Central settings loaded from environment variables / .env file.
Using pydantic-settings means every setting is type-validated at startup —
if DATABASE_URL is missing the app refuses to start rather than failing
silently at the first DB call.
"""

from decimal import Decimal
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Database ──────────────────────────────────────────────────────
    database_url: str = Field(..., description="Async SQLAlchemy URL (asyncpg)")
    database_url_sync: str = Field(..., description="Sync URL for Alembic migrations")

    # ── Redis ─────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ── LLM ──────────────────────────────────────────────────────────
    llm_provider: str = "anthropic"  # "anthropic" | "openai"
    llm_model: str = "claude-opus-4-8"
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None

    # ── Slack ─────────────────────────────────────────────────────────
    slack_bot_token: str | None = None
    slack_signing_secret: str | None = None
    slack_approval_channel: str = "#invoice-approvals"

    # ── LangSmith ─────────────────────────────────────────────────────
    langchain_tracing_v2: bool = False
    langchain_api_key: str | None = None
    langchain_project: str = "invoice-automation"

    # ── App ───────────────────────────────────────────────────────────
    environment: str = "development"
    secret_key: str = "change-me"

    # ── Risk policy thresholds ────────────────────────────────────────
    # Amount (in invoice currency) above which a human must approve.
    # Externalised as a setting so it can be tuned without a code deploy.
    risk_amount_threshold: Decimal = Decimal("10000.00")


# Singleton — import this everywhere instead of constructing a new instance.
settings = Settings()
