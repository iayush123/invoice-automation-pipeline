"""
arq worker — async task queue for invoice processing.

Why a task queue instead of running the graph inline?
──────────────────────────────────────────────────────
The webhook endpoint must return in < 5s (Slack, email services all time out).
LLM extraction can take 10-30s. We respond immediately with 202, enqueue the
work, and let the worker process it asynchronously.

The worker also gives us:
  - Automatic retries with backoff on transient failures.
  - A dead-letter mechanism (max_tries exceeded → dead_letter status).
  - Concurrency control (max_jobs in WorkerSettings).

arq vs Celery:
  arq is purely async (asyncio-native), simpler config, no need for a
  separate result backend. Celery is more featureful but heavier.
  For this workload (moderate throughput, Python async everywhere) arq wins.
"""

import uuid
import structlog
from sqlalchemy import select

from app.core.config import settings
from app.db.base import AsyncSessionLocal
from app.models.invoice import Invoice, InvoiceAuditLog, InvoiceStatus

log = structlog.get_logger()

MAX_TRIES = 3
RETRY_DELAY_SECONDS = 30  # arq uses job_timeout + defer_by for backoff


async def process_invoice(ctx: dict, invoice_id: str) -> dict:
    """
    Main worker task — runs the full LangGraph pipeline for one invoice.

    arq calls this with ctx (worker context) and our payload.
    On unhandled exception arq retries up to MAX_TRIES times.
    """
    log.info("worker_process_start", invoice_id=invoice_id)

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Invoice).where(Invoice.id == invoice_id))
        invoice = result.scalar_one_or_none()

        if not invoice:
            log.error("worker_invoice_not_found", invoice_id=invoice_id)
            return {"error": "not_found"}

        if invoice.status not in (InvoiceStatus.RECEIVED, InvoiceStatus.FAILED):
            # Already being processed or completed — idempotent guard
            log.info("worker_skip_already_processing", invoice_id=invoice_id, status=invoice.status)
            return {"skipped": True}

        # Mark as extracting
        invoice.status = InvoiceStatus.EXTRACTING
        db.add(InvoiceAuditLog(
            invoice_id=invoice.id,
            from_status=InvoiceStatus.RECEIVED,
            to_status=InvoiceStatus.EXTRACTING,
            event="extraction_started",
            actor="worker",
        ))
        await db.commit()

    # Run the graph
    try:
        from app.graph.graph import get_graph_with_checkpointer
        graph = await get_graph_with_checkpointer()

        thread_id = str(uuid.uuid4())
        config = {"configurable": {"thread_id": thread_id}}

        initial_state = {
            "invoice_id": invoice_id,
            "file_path": invoice.raw_storage_path,
            "filename": invoice.filename,
            "retry_count": ctx.get("job_try", 1),
        }

        # Run the graph — it may pause at human_approval interrupt
        result_state = await graph.ainvoke(initial_state, config=config)

        # Persist thread_id so Slack callback can resume
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Invoice).where(Invoice.id == invoice_id))
            inv = result.scalar_one()
            inv.graph_thread_id = thread_id

            # Update status based on graph outcome
            decision = result_state.get("decision")
            extracted = result_state.get("extracted_data")

            if extracted:
                inv.extracted_data = extracted

            if result_state.get("payment_reference"):
                inv.payment_reference = result_state["payment_reference"]
                inv.status = InvoiceStatus.PAID
                db.add(InvoiceAuditLog(
                    invoice_id=inv.id,
                    from_status=InvoiceStatus.EXECUTING,
                    to_status=InvoiceStatus.PAID,
                    event="payment_executed",
                    actor="worker",
                    extra_data={"payment_reference": inv.payment_reference},
                ))
            elif decision == "needs_review":
                inv.status = InvoiceStatus.PENDING_REVIEW
                db.add(InvoiceAuditLog(
                    invoice_id=inv.id,
                    from_status=InvoiceStatus.DECIDING,
                    to_status=InvoiceStatus.PENDING_REVIEW,
                    event="sent_for_review",
                    actor="worker",
                ))
            elif decision == "reject" or result_state.get("extraction_error"):
                inv.status = InvoiceStatus.DEAD_LETTERED
                db.add(InvoiceAuditLog(
                    invoice_id=inv.id,
                    from_status=inv.status,
                    to_status=InvoiceStatus.DEAD_LETTERED,
                    event="dead_lettered",
                    actor="worker",
                    extra_data={"error": result_state.get("extraction_error") or result_state.get("error")},
                ))
            else:
                inv.status = InvoiceStatus.AUTO_APPROVED

            await db.commit()

        log.info("worker_process_complete", invoice_id=invoice_id, decision=decision)
        return {"decision": decision}

    except Exception as e:
        log.error("worker_process_failed", invoice_id=invoice_id, error=str(e))

        # Mark as failed so retry picks it up
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Invoice).where(Invoice.id == invoice_id))
            inv = result.scalar_one_or_none()
            if inv:
                inv.status = InvoiceStatus.FAILED
                db.add(InvoiceAuditLog(
                    invoice_id=inv.id,
                    from_status=inv.status,
                    to_status=InvoiceStatus.FAILED,
                    event="processing_failed",
                    actor="worker",
                    extra_data={"error": str(e)},
                ))
                await db.commit()
        raise  # re-raise so arq retries


async def dead_letter_invoice(ctx: dict, invoice_id: str, error: str) -> None:
    """Called by arq after MAX_TRIES exhausted."""
    log.error("dead_letter_final", invoice_id=invoice_id, error=error)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Invoice).where(Invoice.id == invoice_id))
        inv = result.scalar_one_or_none()
        if inv:
            inv.status = InvoiceStatus.DEAD_LETTERED
            db.add(InvoiceAuditLog(
                invoice_id=inv.id,
                from_status=inv.status,
                to_status=InvoiceStatus.DEAD_LETTERED,
                event="max_retries_exceeded",
                actor="worker",
                extra_data={"error": error},
            ))
            await db.commit()


class WorkerSettings:
    """arq worker configuration."""
    functions = [process_invoice]
    on_job_prerun = []
    redis_settings = None  # set at runtime from settings
    max_jobs = 10
    job_timeout = 120          # seconds before a job is considered stuck
    keep_result = 3600         # keep job results for 1 hour
    retry_jobs = True
    max_tries = MAX_TRIES

    @classmethod
    def build(cls):
        from arq.connections import RedisSettings
        cls.redis_settings = RedisSettings.from_dsn(settings.redis_url)
        return cls
