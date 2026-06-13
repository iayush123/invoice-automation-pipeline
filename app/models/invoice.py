"""
Database models for the invoice pipeline.

Design decisions worth knowing for an interview:
─────────────────────────────────────────────────
1. IDEMPOTENCY KEY (content_hash):
   SHA-256 of the raw file bytes, stored with a UNIQUE constraint.
   If the same PDF is uploaded twice — even via different endpoints,
   even concurrently — the DB constraint fires and we return 409,
   never creating a duplicate payment. This is cheaper and more
   reliable than application-level deduplication.

2. APPEND-ONLY AUDIT LOG (InvoiceAuditLog):
   We never UPDATE audit rows. Every state transition is a new INSERT.
   The DB user could be granted INSERT-only on this table in production.
   This means you can replay history perfectly and detect tampering
   (rows can't be quietly edited to hide a mistake).

3. STATUS AS ENUM:
   Using a Python Enum mapped to a Postgres ENUM keeps invalid statuses
   impossible at the DB layer, not just the application layer.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class InvoiceStatus(str, enum.Enum):
    RECEIVED = "received"
    EXTRACTING = "extracting"
    EXTRACTED = "extracted"
    DECIDING = "deciding"
    AUTO_APPROVED = "auto_approved"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    PAID = "paid"
    FAILED = "failed"
    DEAD_LETTERED = "dead_lettered"


class DecisionOutcome(str, enum.Enum):
    AUTO_APPROVE = "auto_approve"
    NEEDS_REVIEW = "needs_review"
    REJECT = "reject"


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    raw_storage_path: Mapped[str | None] = mapped_column(String(1024))
    source: Mapped[str] = mapped_column(String(64), default="upload")
    extracted_data: Mapped[dict | None] = mapped_column(JSONB)
    decision: Mapped[str | None] = mapped_column(
        Enum(DecisionOutcome, name="decision_outcome")
    )
    decision_reasons: Mapped[list | None] = mapped_column(JSONB)
    payment_reference: Mapped[str | None] = mapped_column(String(256))
    amount_paid: Mapped[float | None] = mapped_column(Numeric(15, 2))
    graph_thread_id: Mapped[str | None] = mapped_column(String(256))
    slack_message_ts: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(
        Enum(InvoiceStatus, name="invoice_status"),
        default=InvoiceStatus.RECEIVED,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    audit_logs: Mapped[list["InvoiceAuditLog"]] = relationship(
        back_populates="invoice", order_by="InvoiceAuditLog.created_at"
    )

    __table_args__ = (
        UniqueConstraint("content_hash", name="uq_invoices_content_hash"),
    )

    def __repr__(self) -> str:
        return f"<Invoice {self.id} status={self.status}>"


class InvoiceAuditLog(Base):
    """Immutable append-only record of every state transition. Never UPDATE rows."""
    __tablename__ = "invoice_audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=False
    )
    from_status: Mapped[str | None] = mapped_column(String(64))
    to_status: Mapped[str] = mapped_column(String(64), nullable=False)
    event: Mapped[str] = mapped_column(String(256), nullable=False)
    actor: Mapped[str] = mapped_column(String(256), default="system")
    extra_data: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    invoice: Mapped["Invoice"] = relationship(back_populates="audit_logs")

    def __repr__(self) -> str:
        return f"<AuditLog {self.invoice_id} {self.from_status}->{self.to_status}>"
