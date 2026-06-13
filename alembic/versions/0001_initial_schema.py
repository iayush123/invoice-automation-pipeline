"""Initial schema — invoices + audit log.

Revision ID: 0001
Revises:
Create Date: 2025-06-01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── invoices ──────────────────────────────────────────────────────
    op.create_table(
        "invoices",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("raw_storage_path", sa.String(1024), nullable=True),
        sa.Column("source", sa.String(64), nullable=False, server_default="upload"),
        sa.Column("extracted_data", postgresql.JSONB(), nullable=True),
        sa.Column("decision", postgresql.ENUM("auto_approve", "needs_review", "reject", name="decision_outcome", create_type=True), nullable=True),
        sa.Column("decision_reasons", postgresql.JSONB(), nullable=True),
        sa.Column("payment_reference", sa.String(256), nullable=True),
        sa.Column("amount_paid", sa.Numeric(15, 2), nullable=True),
        sa.Column("graph_thread_id", sa.String(256), nullable=True),
        sa.Column("slack_message_ts", sa.String(128), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(
                "received", "extracting", "extracted", "deciding",
                "auto_approved", "pending_review", "approved", "rejected",
                "executing", "paid", "failed", "dead_lettered",
                name="invoice_status",
                create_type=True,
            ),
            nullable=False,
            server_default="received",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("content_hash", name="uq_invoices_content_hash"),
    )
    op.create_index("ix_invoices_status", "invoices", ["status"])
    op.create_index("ix_invoices_created_at", "invoices", ["created_at"])

    # ── invoice_audit_logs ────────────────────────────────────────────
    op.create_table(
        "invoice_audit_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_status", sa.String(64), nullable=True),
        sa.Column("to_status", sa.String(64), nullable=False),
        sa.Column("event", sa.String(256), nullable=False),
        sa.Column("actor", sa.String(256), nullable=False, server_default="system"),
        sa.Column("extra_data", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_logs_invoice_id", "invoice_audit_logs", ["invoice_id"])


def downgrade() -> None:
    op.drop_table("invoice_audit_logs")
    op.drop_table("invoices")
    op.execute("DROP TYPE IF EXISTS invoice_status")
    op.execute("DROP TYPE IF EXISTS decision_outcome")
