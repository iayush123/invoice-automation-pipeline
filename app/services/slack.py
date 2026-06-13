"""
Slack service — send approval requests and handle interactive callbacks.

Message design:
  We send a Block Kit message with two action buttons (Approve / Reject).
  The button value encodes the invoice_id so the callback knows which
  graph thread to resume.

Why store the Slack message ts?
─────────────────────────────────
When the invoice is resolved (approved/rejected), we update the original
Slack message to show the outcome. Without the ts (timestamp = message ID),
we'd have to send a new message. Updating in-place keeps the thread clean.
"""

import httpx
import structlog

from app.core.config import settings

log = structlog.get_logger()


async def send_approval_request(
    invoice_id: str,
    vendor: str,
    amount: float,
    currency: str,
    reasons: list[str],
) -> str:
    """
    Post an interactive approval message to the configured Slack channel.

    Returns the message timestamp (ts) which serves as the Slack message ID.
    """
    if not settings.slack_bot_token:
        log.warning("slack_not_configured_using_mock")
        return "mock_ts_" + invoice_id[:8]

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "🧾 Invoice Requires Approval"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Vendor:*\n{vendor}"},
                {"type": "mrkdwn", "text": f"*Amount:*\n{currency} {amount:,.2f}"},
                {"type": "mrkdwn", "text": f"*Invoice ID:*\n`{invoice_id}`"},
                {"type": "mrkdwn", "text": f"*Reason(s):*\n" + "\n".join(f"• {r}" for r in reasons)},
            ],
        },
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Approve"},
                    "style": "primary",
                    "action_id": "invoice_approve",
                    "value": invoice_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ Reject"},
                    "style": "danger",
                    "action_id": "invoice_reject",
                    "value": invoice_id,
                },
            ],
        },
    ]

    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {settings.slack_bot_token}"},
            json={
                "channel": settings.slack_approval_channel,
                "blocks": blocks,
                "text": f"Invoice from {vendor} ({currency} {amount:,.2f}) requires approval.",
            },
        )
        data = r.json()

    if not data.get("ok"):
        raise RuntimeError(f"Slack API error: {data.get('error')}")

    ts = data["ts"]
    log.info("slack_approval_sent", invoice_id=invoice_id, ts=ts)
    return ts


async def update_approval_message(ts: str, outcome: str, actor: str) -> None:
    """Update the original Slack message to show resolution."""
    if not settings.slack_bot_token or ts.startswith("mock_ts_"):
        return

    emoji = "✅" if outcome == "approve" else "❌"
    label = "Approved" if outcome == "approve" else "Rejected"

    async with httpx.AsyncClient() as client:
        await client.post(
            "https://slack.com/api/chat.update",
            headers={"Authorization": f"Bearer {settings.slack_bot_token}"},
            json={
                "channel": settings.slack_approval_channel,
                "ts": ts,
                "text": f"{emoji} Invoice {label} by <@{actor}>",
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"{emoji} *{label}* by <@{actor}>",
                        },
                    }
                ],
            },
        )
