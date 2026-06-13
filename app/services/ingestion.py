"""
Ingestion service — the entry point for every invoice.

Core responsibilities:
1. Compute a SHA-256 content hash (idempotency key).
2. Attempt to INSERT a new Invoice row.
3. If the DB raises IntegrityError (duplicate hash) → return the existing row.
4. Persist the raw file bytes to local disk (swap for S3/GCS in production).
5. Write the first audit log entry.

Why content hash rather than filename or invoice number?
─────────────────────────────────────────────────────────
Filenames are easily duplicated (invoice.pdf sent twice). Invoice numbers
are extracted by the LLM and might not be available at ingestion time — and
could theoretically be wrong if the LLM hallucinates. The raw file bytes are
the ground truth: if the bytes are identical, it IS the same document.
"""

import hashlib
import os
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invoice import Invoice, InvoiceAuditLog, InvoiceStatus

# Raw uploads stored under this directory.
# In production this would be replaced with an object-store (S3/GCS) path.
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "/tmp/invoice_uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def compute_content_hash(data: bytes) -> str:
    """SHA-256 hex digest of raw file bytes."""
    return hashlib.sha256(data).hexdigest()


async def ingest_invoice(
    db: AsyncSession,
    file_bytes: bytes,
    filename: str,
    source: str = "upload",
) -> tuple[Invoice, bool]:
    """
    Ingest an invoice file.

    Returns:
        (invoice, created): created=True if new, False if duplicate.

    The caller should check `created` and return HTTP 200 vs 409 accordingly.
    We never raise on duplicates — we return the existing record so the caller
    can tell the user exactly which invoice this duplicates.
    """
    content_hash = compute_content_hash(file_bytes)

    # Persist raw bytes before touching the DB so that if the write fails
    # we don't have a DB row pointing at a missing file.
    storage_path = UPLOAD_DIR / f"{content_hash}_{filename}"
    if not storage_path.exists():
        storage_path.write_bytes(file_bytes)

    invoice = Invoice(
        id=uuid.uuid4(),
        content_hash=content_hash,
        filename=filename,
        raw_storage_path=str(storage_path),
        source=source,
        status=InvoiceStatus.RECEIVED,
    )

    try:
        db.add(invoice)
        await db.flush()  # flush to get constraint check before commit

        audit = InvoiceAuditLog(
            invoice_id=invoice.id,
            from_status=None,
            to_status=InvoiceStatus.RECEIVED,
            event="invoice_received",
            actor="system",
            extra_data={"filename": filename, "source": source, "size_bytes": len(file_bytes)},
        )
        db.add(audit)
        await db.commit()
        await db.refresh(invoice)
        return invoice, True

    except IntegrityError:
        # Duplicate content_hash — roll back and fetch the existing row.
        await db.rollback()
        result = await db.execute(
            select(Invoice).where(Invoice.content_hash == content_hash)
        )
        existing = result.scalar_one()
        return existing, False
