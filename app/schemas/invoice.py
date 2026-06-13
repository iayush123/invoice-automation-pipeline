"""
Pydantic v2 schemas — the contract between API, LLM output, and internal services.

InvoiceExtracted is the schema the LLM must produce. Making it strict (no extra
fields, validated types) means a malformed LLM response fails fast with a clear
error rather than silently propagating bad data downstream.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


# ── LLM extraction output ──────────────────────────────────────────────────────

class LineItem(BaseModel):
    description: str
    quantity: Decimal = Field(ge=0)
    unit_price: Decimal = Field(ge=0)
    total: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def check_total(self) -> "LineItem":
        expected = (self.quantity * self.unit_price).quantize(Decimal("0.01"))
        if abs(self.total - expected) > Decimal("0.02"):
            raise ValueError(
                f"Line item total {self.total} doesn't match "
                f"qty×price={expected} (tolerance 0.02)"
            )
        return self


class InvoiceExtracted(BaseModel):
    """
    Structured data the LLM must return for every invoice.
    All fields are required — if the LLM can't find a value it must say so
    with a clear null/None rather than hallucinating.
    """
    vendor_name: str
    vendor_address: str | None = None
    invoice_number: str
    invoice_date: date
    due_date: date | None = None
    currency: str = Field(default="USD", max_length=3)
    subtotal: Decimal = Field(ge=0)
    tax: Decimal = Field(default=Decimal("0"), ge=0)
    total: Decimal = Field(ge=0)
    line_items: list[LineItem] = Field(default_factory=list)
    purchase_order_number: str | None = None  # PO for mismatch detection
    notes: str | None = None

    @field_validator("currency")
    @classmethod
    def currency_upper(cls, v: str) -> str:
        return v.upper()

    @model_validator(mode="after")
    def check_total_matches_subtotal(self) -> "InvoiceExtracted":
        expected = (self.subtotal + self.tax).quantize(Decimal("0.01"))
        if abs(self.total - expected) > Decimal("0.05"):
            raise ValueError(
                f"Invoice total {self.total} doesn't match "
                f"subtotal+tax={expected} (tolerance 0.05)"
            )
        return self


# ── API request/response schemas ───────────────────────────────────────────────

class InvoiceUploadResponse(BaseModel):
    invoice_id: uuid.UUID
    content_hash: str
    status: str
    message: str


class InvoiceStatusResponse(BaseModel):
    invoice_id: uuid.UUID
    status: str
    decision: str | None
    decision_reasons: list[str] | None
    extracted_data: dict[str, Any] | None
    payment_reference: str | None
    created_at: datetime
    updated_at: datetime


class WebhookEmailPayload(BaseModel):
    """Simulated email-webhook payload (what an email-parsing service would send)."""
    sender_email: str
    subject: str
    body: str | None = None
    # base64-encoded attachment
    attachment_base64: str
    attachment_filename: str
    attachment_content_type: str = "application/pdf"


class AuditLogEntry(BaseModel):
    id: int
    invoice_id: uuid.UUID
    from_status: str | None
    to_status: str
    event: str
    actor: str
    extra_data: dict[str, Any] | None
    created_at: datetime
