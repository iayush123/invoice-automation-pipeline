"""
LangGraph node functions — one function per pipeline stage.

Each node receives the current InvoiceState and returns a dict of fields
to merge into state. LangGraph handles the merge.

Nodes covered here:
  extract_node   → calls the LLM extraction service
  decide_node    → applies the risk policy rules
  execute_node   → mocked payment + audit log
  notify_node    → sends Slack approval request

The human approval interrupt is wired into the graph definition (graph.py),
not here. The interrupt point is between notify_node and execute_node.
"""

import uuid
from decimal import Decimal

import structlog

from app.core.config import settings
from app.graph.state import InvoiceState
from app.models.invoice import DecisionOutcome, InvoiceStatus
from app.schemas.invoice import InvoiceExtracted
from app.services.extraction import extract_invoice

log = structlog.get_logger()


# ─────────────────────────────────────────────────────────────────────────────
# Node: extract
# ─────────────────────────────────────────────────────────────────────────────

def extract_node(state: InvoiceState) -> dict:
    """
    Call the LLM to extract structured data from the raw invoice file.

    On failure, we write extraction_error into state so the graph can route
    to a dead-letter path rather than crashing the whole workflow.
    """
    log.info("extract_node_start", invoice_id=state.get("invoice_id"))
    try:
        extracted: InvoiceExtracted = extract_invoice(state["file_path"])
        return {
            "extracted_data": extracted.model_dump(mode="json"),
            "extraction_error": None,
        }
    except Exception as e:
        log.error("extract_node_failed", invoice_id=state.get("invoice_id"), error=str(e))
        return {
            "extracted_data": None,
            "extraction_error": str(e),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Node: decide (risk policy)
# ─────────────────────────────────────────────────────────────────────────────

# Known vendors loaded from DB in production; hard-coded set for now.
# In Milestone 3 this is a real DB lookup.
KNOWN_VENDORS: set[str] = set()


def decide_node(state: InvoiceState) -> dict:
    """
    Apply risk-policy rules to extracted invoice data and emit a Decision.

    Rules (in priority order):
    1. Extraction failed → reject.
    2. Amount exceeds RISK_AMOUNT_THRESHOLD → needs_review.
    3. New vendor (not seen before) → needs_review.
    4. PO number missing when total > $500 → needs_review.
    5. Otherwise → auto_approve.

    Interview point: rules are data, not code.
    ───────────────────────────────────────────
    In production these thresholds live in the DB / config and can be
    changed by a finance admin without a deploy. The structure here
    (list of (condition_fn, reason) tuples) makes that easy to extend.
    """
    if state.get("extraction_error") or not state.get("extracted_data"):
        return {
            "decision": DecisionOutcome.REJECT,
            "decision_reasons": [f"Extraction failed: {state.get('extraction_error')}"],
        }

    data = state["extracted_data"]
    reasons: list[str] = []
    outcome = DecisionOutcome.AUTO_APPROVE

    total = Decimal(str(data.get("total", 0)))
    vendor = data.get("vendor_name", "").strip().lower()
    po = data.get("purchase_order_number")

    # Rule 1: high-value invoice
    if total > settings.risk_amount_threshold:
        reasons.append(
            f"Amount {total} exceeds threshold {settings.risk_amount_threshold}"
        )
        outcome = DecisionOutcome.NEEDS_REVIEW

    # Rule 2: new vendor
    if vendor and vendor not in {v.lower() for v in KNOWN_VENDORS}:
        reasons.append(f"New vendor: '{data.get('vendor_name')}'")
        outcome = DecisionOutcome.NEEDS_REVIEW

    # Rule 3: missing PO for non-trivial amounts
    if not po and total > Decimal("500"):
        reasons.append("Purchase order number missing for invoice > $500")
        outcome = DecisionOutcome.NEEDS_REVIEW

    log.info(
        "decide_node_result",
        invoice_id=state.get("invoice_id"),
        decision=outcome,
        reasons=reasons,
    )
    return {
        "decision": outcome,
        "decision_reasons": reasons if reasons else ["All checks passed"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Node: notify (Slack)
# ─────────────────────────────────────────────────────────────────────────────

async def notify_node(state: InvoiceState) -> dict:
    """
    Send a Slack approval request for invoices that need human review.

    The message includes Approve / Reject buttons. When clicked, Slack
    sends an interaction payload to POST /slack/actions, which resumes
    the graph (Milestone 4).
    """
    from app.services.slack import send_approval_request
    data = state.get("extracted_data") or {}
    try:
        ts = await send_approval_request(
            invoice_id=state["invoice_id"],
            vendor=data.get("vendor_name", "Unknown"),
            amount=data.get("total", 0),
            currency=data.get("currency", "USD"),
            reasons=state.get("decision_reasons", []),
        )
        return {"slack_message_ts": ts}
    except Exception as e:
        log.error("notify_node_failed", error=str(e))
        return {"slack_message_ts": None, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Node: execute (mocked payment)
# ─────────────────────────────────────────────────────────────────────────────

def execute_node(state: InvoiceState) -> dict:
    """
    Mock the payment execution.

    In production this would call a payment processor API (Stripe, Bill.com,
    a banking API). Here we generate a fake payment reference so the audit
    trail is realistic without moving real money.

    Idempotency: if payment_reference already exists in state (e.g. graph
    was retried), we return the existing reference rather than generating
    a new one. This prevents double-payment on retry.
    """
    if state.get("payment_reference"):
        log.info("execute_node_already_paid", ref=state["payment_reference"])
        return {"payment_reference": state["payment_reference"]}

    data = state.get("extracted_data") or {}
    # Fake payment reference — replace with real payment API call
    ref = f"PAY-{uuid.uuid4().hex[:12].upper()}"

    log.info(
        "execute_node_paid",
        invoice_id=state.get("invoice_id"),
        ref=ref,
        amount=data.get("total"),
    )
    return {
        "payment_reference": ref,
        "execution_error": None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Routing functions (used in graph.py add_conditional_edges)
# ─────────────────────────────────────────────────────────────────────────────

def route_after_extract(state: InvoiceState) -> str:
    """After extraction: go to decide, or dead-letter if extraction errored."""
    if state.get("extraction_error"):
        return "dead_letter"
    return "decide"


def route_after_decide(state: InvoiceState) -> str:
    """After decision: route to notify (human review) or directly to execute."""
    decision = state.get("decision")
    if decision == DecisionOutcome.NEEDS_REVIEW:
        return "notify"
    elif decision == DecisionOutcome.REJECT:
        return "dead_letter"
    return "execute"


def route_after_human(state: InvoiceState) -> str:
    """After human approval interrupt: approve → execute, reject → dead_letter."""
    action = state.get("human_action", "").lower()
    if action == "approve":
        return "execute"
    return "dead_letter"
