"""mail evidence and analysis context semantics

Revision ID: 0027_mail_evidence_context
Revises: 0026_transaction_links, 0026_portfolio_plan
Create Date: 2026-06-09

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0027_mail_evidence_context"
down_revision: str | Sequence[str] | None = (
    "0026_transaction_links",
    "0026_portfolio_plan",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "categories",
        sa.Column("kind", sa.String(length=32), server_default="expense", nullable=False),
    )
    op.add_column(
        "categories",
        sa.Column("budgetable", sa.Boolean(), server_default="true", nullable=False),
    )
    op.add_column("categories", sa.Column("analysis_group", sa.String(length=64), nullable=True))
    op.add_column("categories", sa.Column("description", sa.String(length=500), nullable=True))
    op.add_column("categories", sa.Column("examples", sa.JSON(), nullable=True))
    op.add_column("categories", sa.Column("anti_examples", sa.JSON(), nullable=True))
    op.add_column("categories", sa.Column("llm_hints", sa.String(length=1000), nullable=True))

    op.add_column(
        "budgets",
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
    )
    op.add_column(
        "budgets",
        sa.Column("priority", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "budgets",
        sa.Column(
            "warning_threshold",
            sa.Numeric(4, 2),
            server_default="0.80",
            nullable=False,
        ),
    )
    op.add_column(
        "budgets",
        sa.Column(
            "critical_threshold",
            sa.Numeric(4, 2),
            server_default="1.00",
            nullable=False,
        ),
    )
    op.add_column("budgets", sa.Column("context_note", sa.String(length=500), nullable=True))

    op.create_table(
        "mail_evidence",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("evidence_type", sa.String(length=32), nullable=False),
        sa.Column("merchant_name", sa.String(length=200), nullable=True),
        sa.Column("merchant_key", sa.String(length=200), nullable=True),
        sa.Column("document_date", sa.Date(), nullable=True),
        sa.Column("total_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("payment_method", sa.String(length=32), nullable=True),
        sa.Column("payment_hint", sa.String(length=200), nullable=True),
        sa.Column("order_ref_hash", sa.String(length=64), nullable=True),
        sa.Column("subject_hint", sa.String(length=200), nullable=True),
        sa.Column("redacted_snippet", sa.String(length=500), nullable=True),
        sa.Column("line_items", sa.JSON(), nullable=True),
        sa.Column("confidence", sa.Numeric(4, 3), server_default="0.000", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source",
            "order_ref_hash",
            "evidence_type",
            name="uq_mail_evidence_source_order_type",
        ),
    )
    op.create_index("ix_mail_evidence_document_date", "mail_evidence", ["document_date"])
    op.create_index("ix_mail_evidence_merchant_key", "mail_evidence", ["merchant_key"])
    op.create_index("ix_mail_evidence_order_ref_hash", "mail_evidence", ["order_ref_hash"])

    op.create_table(
        "transaction_evidence_links",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("transaction_id", sa.String(length=64), nullable=False),
        sa.Column("evidence_id", sa.String(length=64), nullable=False),
        sa.Column("match_type", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=False),
        sa.Column("match_reason", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["evidence_id"],
            ["mail_evidence.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["normalized_transactions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "transaction_id",
            "evidence_id",
            "match_type",
            name="uq_transaction_evidence_link",
        ),
    )
    op.create_index(
        "ix_transaction_evidence_links_evidence",
        "transaction_evidence_links",
        ["evidence_id"],
    )
    op.create_index(
        "ix_transaction_evidence_links_transaction",
        "transaction_evidence_links",
        ["transaction_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_transaction_evidence_links_transaction",
        table_name="transaction_evidence_links",
    )
    op.drop_index(
        "ix_transaction_evidence_links_evidence",
        table_name="transaction_evidence_links",
    )
    op.drop_table("transaction_evidence_links")

    op.drop_index("ix_mail_evidence_order_ref_hash", table_name="mail_evidence")
    op.drop_index("ix_mail_evidence_merchant_key", table_name="mail_evidence")
    op.drop_index("ix_mail_evidence_document_date", table_name="mail_evidence")
    op.drop_table("mail_evidence")

    op.drop_column("budgets", "context_note")
    op.drop_column("budgets", "critical_threshold")
    op.drop_column("budgets", "warning_threshold")
    op.drop_column("budgets", "priority")
    op.drop_column("budgets", "is_active")

    op.drop_column("categories", "llm_hints")
    op.drop_column("categories", "anti_examples")
    op.drop_column("categories", "examples")
    op.drop_column("categories", "description")
    op.drop_column("categories", "analysis_group")
    op.drop_column("categories", "budgetable")
    op.drop_column("categories", "kind")
