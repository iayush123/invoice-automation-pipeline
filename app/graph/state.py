"""
LangGraph state definition for the invoice pipeline.

The State is a TypedDict — LangGraph serialises it to the Postgres
checkpointer so a paused graph (waiting for human approval) can be resumed
after a process restart. Every field must be JSON-serialisable.

Interview point — why TypedDict instead of Pydantic?
──────────────────────────────────────────────────────
LangGraph's checkpointer serialises state via msgpack/JSON. TypedDict
integrates more cleanly with LangGraph's annotation system (Annotated fields
for reducer functions). We validate at the service layer (Pydantic schemas)
before writing into state, so TypedDict here is fine.
"""

from __future__ import annotations

import uuid
from typing import Any, TypedDict


class InvoiceState(TypedDict, total=False):
    # ── Input ──────────────────────────────────────────────────────────
    invoice_id: str           # UUID as string (JSON-safe)
    file_path: str            # absolute path to raw file on disk
    filename: str

    # ── Extraction output ─────────────────────────────────────────────
    extracted_data: dict[str, Any] | None   # InvoiceExtracted.model_dump()
    extraction_error: str | None

    # ── Decision ──────────────────────────────────────────────────────
    decision: str | None      # "auto_approve" | "needs_review" | "reject"
    decision_reasons: list[str]

    # ── Human approval ────────────────────────────────────────────────
    slack_message_ts: str | None    # Slack message timestamp (for updates)
    human_action: str | None        # "approve" | "reject" (filled on resume)
    human_actor: str | None         # Slack user ID who acted

    # ── Execution ─────────────────────────────────────────────────────
    payment_reference: str | None
    execution_error: str | None

    # ── Meta ──────────────────────────────────────────────────────────
    error: str | None               # fatal graph error
    retry_count: int                # current retry attempt
