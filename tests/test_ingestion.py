"""
Milestone 1 tests — ingestion + idempotency.

These are integration tests that hit a real (test) database.
Run with: pytest tests/test_ingestion.py -v

Prerequisites:
  export DATABASE_URL=postgresql+asyncpg://invoice:invoice@localhost:5432/invoice_test
  export DATABASE_URL_SYNC=postgresql+psycopg2://invoice:invoice@localhost:5432/invoice_test
  alembic upgrade head
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.main import app


# ── Fixtures ───────────────────────────────────────────────────────────────────

SAMPLE_PDF_BYTES = b"%PDF-1.4 fake invoice content for testing"
DUPLICATE_PDF_BYTES = SAMPLE_PDF_BYTES  # same bytes → same hash
DIFFERENT_PDF_BYTES = b"%PDF-1.4 a completely different invoice"


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


# ── Tests ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_upload_new_invoice(client):
    """Uploading a new invoice returns 202 and a new invoice_id."""
    r = await client.post(
        "/invoices/upload",
        files={"file": ("invoice_001.pdf", SAMPLE_PDF_BYTES, "application/pdf")},
    )
    assert r.status_code == 202, r.text
    data = r.json()
    assert "invoice_id" in data
    assert data["status"] == "received"
    assert len(data["content_hash"]) == 64  # SHA-256 hex


@pytest.mark.asyncio
async def test_duplicate_upload_rejected(client):
    """
    Uploading the same bytes a second time must return 409.
    This is the core idempotency guarantee — test it explicitly.
    """
    # First upload succeeds
    r1 = await client.post(
        "/invoices/upload",
        files={"file": ("invoice_dup.pdf", DUPLICATE_PDF_BYTES, "application/pdf")},
    )
    # Second identical upload → 409
    r2 = await client.post(
        "/invoices/upload",
        files={"file": ("invoice_dup_renamed.pdf", DUPLICATE_PDF_BYTES, "application/pdf")},
    )
    assert r2.status_code == 409, r2.text
    body = r2.json()
    assert "existing_invoice_id" in body["detail"]


@pytest.mark.asyncio
async def test_different_invoices_both_accepted(client):
    """Two different files should both be accepted."""
    r1 = await client.post(
        "/invoices/upload",
        files={"file": ("inv_a.pdf", SAMPLE_PDF_BYTES + b"_A", "application/pdf")},
    )
    r2 = await client.post(
        "/invoices/upload",
        files={"file": ("inv_b.pdf", SAMPLE_PDF_BYTES + b"_B", "application/pdf")},
    )
    assert r1.status_code == 202
    assert r2.status_code == 202
    assert r1.json()["invoice_id"] != r2.json()["invoice_id"]


@pytest.mark.asyncio
async def test_get_invoice_status(client):
    """After upload, GET /invoices/{id} should return the invoice."""
    r_upload = await client.post(
        "/invoices/upload",
        files={"file": ("inv_status.pdf", b"%PDF status test", "application/pdf")},
    )
    invoice_id = r_upload.json()["invoice_id"]

    r_get = await client.get(f"/invoices/{invoice_id}")
    assert r_get.status_code == 200
    data = r_get.json()
    assert data["status"] == "received"
    assert data["invoice_id"] == invoice_id


@pytest.mark.asyncio
async def test_audit_log_created_on_upload(client):
    """Every uploaded invoice must have at least one audit log entry."""
    r_upload = await client.post(
        "/invoices/upload",
        files={"file": ("inv_audit.pdf", b"%PDF audit test content", "application/pdf")},
    )
    invoice_id = r_upload.json()["invoice_id"]

    r_audit = await client.get(f"/invoices/{invoice_id}/audit")
    assert r_audit.status_code == 200
    logs = r_audit.json()
    assert len(logs) >= 1
    assert logs[0]["event"] == "invoice_received"
    assert logs[0]["to_status"] == "received"
    assert logs[0]["from_status"] is None


@pytest.mark.asyncio
async def test_unsupported_file_type(client):
    """Non-PDF/image uploads must be rejected with 415."""
    r = await client.post(
        "/invoices/upload",
        files={"file": ("invoice.exe", b"MZ malware", "application/octet-stream")},
    )
    assert r.status_code == 415


@pytest.mark.asyncio
async def test_email_webhook(client):
    """Simulated email webhook ingests base64-encoded attachment."""
    import base64
    payload = {
        "sender_email": "vendor@example.com",
        "subject": "Invoice #INV-2025-001",
        "attachment_base64": base64.b64encode(b"%PDF email invoice content").decode(),
        "attachment_filename": "email_invoice.pdf",
    }
    r = await client.post("/invoices/webhook/email", json=payload)
    assert r.status_code == 202
    assert r.json()["status"] == "received"
