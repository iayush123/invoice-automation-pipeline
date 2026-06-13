"""
Slack interactive-action callback endpoint.

Slack sends a POST to this endpoint when a user clicks Approve or Reject
in the approval message. We verify the request signature, parse the payload,
then resume the LangGraph graph with the human's decision.

Interview point — graph resume mechanics:
──────────────────────────────────────────
1. Look up the invoice by ID to get its graph_thread_id.
2. Call graph.ainvoke({"human_action": action}, {"configurable": {"thread_id": thread_id}}).
3. LangGraph fetches the checkpointed state from Postgres, merges the new
   human_action value, and continues from the human_approval node onward.
4. The graph resumes synchronously from our perspective — by the time
   ainvoke returns, the invoice has been paid (or dead-lettered).
"""

import hashlib
import hmac
import json
import time
import urllib.parse

import structlog
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from app.core.config import settings
from app.db.base import AsyncSessionLocal
from app.models.invoice import Invoice, InvoiceAuditLog, InvoiceStatus
from app.services.slack import update_approval_message

router = APIRouter(prefix="/slack", tags=["slack"])
log = structlog.get_logger()


def _verify_slack_signature(body: bytes, timestamp: str, signature: str) -> bool:
    """
    Verify the request came from Slack using HMAC-SHA256.
    https://api.slack.com/authentication/verifying-requests-from-slack
    """
    if not settings.slack_signing_secret:
        return True  # skip in development if secret not configured

    # Reject replays older than 5 minutes
    if abs(time.time() - float(timestamp)) > 300:
        return False

    base = f"v0:{timestamp}:{body.decode()}"
    expected = "v0=" + hmac.new(
        key=settings.slack_signing_secret.encode(),
        msg=base.encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/actions")
async def slack_actions(request: Request):
    """
    Handle Slack interactive component payloads (button clicks).
    """
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    if not _verify_slack_signature(body, timestamp, signature):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    # Slack sends payload as URL-encoded form data
    form_data = urllib.parse.parse_qs(body.decode())
    payload = json.loads(form_data.get("payload", ["{}"])[0])

    action = payload.get("actions", [{}])[0]
    action_id = action.get("action_id")  # "invoice_approve" | "invoice_reject"
    invoice_id = action.get("value")
    slack_user_id = payload.get("user", {}).get("id", "unknown")

    if action_id not in ("invoice_approve", "invoice_reject"):
        return {"ok": True}  # unknown action, ignore

    human_action = "approve" if action_id == "invoice_approve" else "reject"

    log.info(
        "slack_action_received",
        invoice_id=invoice_id,
        action=human_action,
        user=slack_user_id,
    )

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Invoice).where(Invoice.id == invoice_id)
        )
        invoice = result.scalar_one_or_none()

        if not invoice:
            log.error("slack_action_invoice_not_found", invoice_id=invoice_id)
            return {"ok": True}

        # Resume the LangGraph workflow
        from app.graph.graph import get_graph_with_checkpointer
        graph = await get_graph_with_checkpointer()

        thread_id = invoice.graph_thread_id
        config = {"configurable": {"thread_id": thread_id}}

        try:
            await graph.ainvoke(
                {"human_action": human_action, "human_actor": slack_user_id},
                config=config,
            )
        except Exception as e:
            log.error("graph_resume_failed", error=str(e), invoice_id=invoice_id)
            raise HTTPException(status_code=500, detail="Failed to resume workflow")

        # Update invoice status
        new_status = InvoiceStatus.APPROVED if human_action == "approve" else InvoiceStatus.REJECTED
        old_status = invoice.status
        invoice.status = new_status

        audit = InvoiceAuditLog(
            invoice_id=invoice.id,
            from_status=old_status,
            to_status=new_status,
            event=f"human_{human_action}",
            actor=slack_user_id,
            extra_data={"slack_user_id": slack_user_id},
        )
        db.add(audit)
        await db.commit()

        # Update the Slack message to show outcome
        if invoice.slack_message_ts:  # type: ignore[attr-defined]
            await update_approval_message(
                ts=invoice.slack_message_ts,  # type: ignore[attr-defined]
                outcome=human_action,
                actor=slack_user_id,
            )

    # Slack expects a 200 response immediately
    return {"ok": True}
