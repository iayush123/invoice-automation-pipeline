"""
Invoice API endpoints.

POST /invoices/upload        — direct file upload
POST /invoices/webhook/email — simulated email-service webhook
GET  /invoices/{id}          — status + extracted data
GET  /invoices/{id}/audit    — full audit trail
"""

import base64
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_db
from app.models.invoice import Invoice, InvoiceAuditLog
from app.schemas.invoice import (
    AuditLogEntry,
    InvoiceStatusResponse,
    InvoiceUploadResponse,
    WebhookEmailPayload,
)
from app.services.ingestion import ingest_invoice

router = APIRouter(prefix="/invoices", tags=["invoices"])

ALLOWED_CONTENT_TYPES = {"application/pdf", "image/png", "image/jpeg", "image/tiff"}
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB


@router.post("/upload", response_model=InvoiceUploadResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload_invoice(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """
    Upload an invoice file (PDF or image).

    Returns 202 Accepted — processing is async.
    Returns 409 Conflict if this exact file has been submitted before.
    """
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type: {file.content_type}. "
                   f"Accepted: {ALLOWED_CONTENT_TYPES}",
        )

    file_bytes = await file.read()
    if len(file_bytes) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File exceeds 20 MB limit.")

    invoice, created = await ingest_invoice(
        db=db,
        file_bytes=file_bytes,
        filename=file.filename or "unknown.pdf",
        source="upload",
    )

    if not created:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Duplicate invoice — this file has already been submitted.",
                "existing_invoice_id": str(invoice.id),
                "existing_status": invoice.status,
            },
        )

    return InvoiceUploadResponse(
        invoice_id=invoice.id,
        content_hash=invoice.content_hash,
        status=invoice.status,
        message="Invoice received. Extraction will begin shortly.",
    )


@router.post(
    "/webhook/email",
    response_model=InvoiceUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def email_webhook(
    payload: WebhookEmailPayload,
    db: AsyncSession = Depends(get_db),
):
    """
    Simulated email-parsing-service webhook.

    In production this would be called by a service like Nylas or SendGrid
    Inbound Parse. It sends the attachment as base64 so we don't need to
    open a mailbox.
    """
    try:
        file_bytes = base64.b64decode(payload.attachment_base64)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 attachment data.")

    invoice, created = await ingest_invoice(
        db=db,
        file_bytes=file_bytes,
        filename=payload.attachment_filename,
        source="email",
    )

    if not created:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Duplicate invoice — this file has already been processed.",
                "existing_invoice_id": str(invoice.id),
            },
        )

    return InvoiceUploadResponse(
        invoice_id=invoice.id,
        content_hash=invoice.content_hash,
        status=invoice.status,
        message=f"Invoice received via email from {payload.sender_email}.",
    )


@router.get("/{invoice_id}", response_model=InvoiceStatusResponse)
async def get_invoice(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Invoice).where(Invoice.id == invoice_id))
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found.")

    return InvoiceStatusResponse(
        invoice_id=invoice.id,
        status=invoice.status,
        decision=invoice.decision,
        decision_reasons=invoice.decision_reasons,
        extracted_data=invoice.extracted_data,
        payment_reference=invoice.payment_reference,
        created_at=invoice.created_at,
        updated_at=invoice.updated_at,
    )


@router.get("/{invoice_id}/audit", response_model=list[AuditLogEntry])
async def get_audit_log(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Return the full immutable audit trail for this invoice."""
    result = await db.execute(
        select(InvoiceAuditLog)
        .where(InvoiceAuditLog.invoice_id == invoice_id)
        .order_by(InvoiceAuditLog.created_at)
    )
    logs = result.scalars().all()
    if not logs:
        inv_result = await db.execute(select(Invoice).where(Invoice.id == invoice_id))
        if not inv_result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Invoice not found.")
    return [
        AuditLogEntry(
            id=log.id,
            invoice_id=log.invoice_id,
            from_status=log.from_status,
            to_status=log.to_status,
            event=log.event,
            actor=log.actor,
            extra_data=log.extra_data,
            created_at=log.created_at,
        )
        for log in logs
    ]
