"""
Audit log service — centralises all writes to the immutable audit log.

Every state transition in the system goes through this module.
Never update existing audit rows. Every change = a new INSERT.
"""

import uuid
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invoice import Invoice, InvoiceAuditLog, InvoiceStatus

log = structlog.get_logger()


async def transition(
    db: AsyncSession,
    invoice: Invoice,
    to_status: InvoiceStatus,
    event: str,
    actor: str = "system",
    extra_data: dict[str, Any] | None = None,
) -> InvoiceAuditLog:
    """
    Transition an invoice to a new status and write an audit log entry atomically.

    Usage:
        await audit.transition(db, invoice, InvoiceStatus.PAID, "payment_executed",
                               actor="worker", extra_data={"ref": "PAY-ABC123"})
    """
    from_status = invoice.status
    invoice.status = to_status

    entry = InvoiceAuditLog(
        invoice_id=invoice.id,
        from_status=from_status,
        to_status=to_status,
        event=event,
        actor=actor,
        extra_data=extra_data,
    )
    db.add(entry)

    log.info(
        "audit_transition",
        invoice_id=str(invoice.id),
        from_status=from_status,
        to_status=to_status,
        event=event,
        actor=actor,
    )
    return entry
