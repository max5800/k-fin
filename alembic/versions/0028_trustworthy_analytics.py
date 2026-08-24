"""version trustworthy analytics and reversible normalization state

Revision ID: 0028_trustworthy_analytics
Revises: 0027_mail_evidence_context
Create Date: 2026-08-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0028_trustworthy_analytics"
down_revision: str | Sequence[str] | None = "0027_mail_evidence_context"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "normalized_transactions",
        sa.Column("normalization_version", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "normalized_transactions",
        sa.Column("normalization_status", sa.String(32), server_default="active", nullable=False),
    )
    op.add_column(
        "normalized_transactions",
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
    )
    op.add_column(
        "normalized_transactions", sa.Column("superseded_by_id", sa.String(64), nullable=True)
    )
    op.add_column(
        "normalized_transactions",
        sa.Column(
            "accounting_class",
            sa.String(48),
            server_default="unresolved_ambiguous",
            nullable=False,
        ),
    )
    op.add_column(
        "normalized_transactions",
        sa.Column("accounting_confidence", sa.Numeric(4, 3), server_default="0.000", nullable=False),
    )
    op.add_column(
        "normalized_transactions",
        sa.Column("accounting_version", sa.Integer(), server_default="2", nullable=False),
    )
    op.add_column(
        "normalized_transactions",
        sa.Column(
            "refund_verification_status",
            sa.String(32),
            server_default="unverified",
            nullable=False,
        ),
    )
    op.create_index(
        "ix_normalized_transactions_is_active",
        "normalized_transactions",
        ["is_active"],
    )
    op.create_index(
        "ix_normalized_transactions_accounting_class",
        "normalized_transactions",
        ["accounting_class"],
    )

    op.add_column(
        "transaction_links",
        sa.Column("status", sa.String(32), server_default="active", nullable=False),
    )
    op.add_column(
        "transaction_links",
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
    )
    op.add_column(
        "transaction_links", sa.Column("version", sa.Integer(), server_default="1", nullable=False)
    )
    op.add_column(
        "transaction_links",
        sa.Column("confidence", sa.Numeric(4, 3), server_default="1.000", nullable=False),
    )
    op.add_column(
        "transaction_links", sa.Column("match_reason", sa.String(200), nullable=True)
    )
    op.create_index("ix_transaction_links_is_active", "transaction_links", ["is_active"])

    op.create_table(
        "source_statement_periods",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "source",
            postgresql.ENUM(
                "comdirect", "paypal", "santander_cc", name="data_source", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("rows_present", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("observed_row_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("verified_complete", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("verification_method", sa.String(64), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("source", "period_start", "period_end", name="uq_source_period"),
    )
    op.create_index(
        "ix_source_statement_periods_period_start", "source_statement_periods", ["period_start"]
    )

    op.create_table(
        "subscription_records",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=False),
        sa.Column("evidence_source", sa.String(32), nullable=False),
        sa.Column("transaction_id", sa.String(64), sa.ForeignKey("normalized_transactions.id"), nullable=True),
        sa.Column("amount_scenarios", sa.JSON(), nullable=True),
        sa.Column("next_review_date", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_subscription_records_status", "subscription_records", ["status"])
    op.create_index(
        "ix_subscription_records_transaction_id", "subscription_records", ["transaction_id"]
    )

    op.create_table(
        "value_assessments",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("transaction_id", sa.String(64), sa.ForeignKey("normalized_transactions.id"), nullable=False, unique=True),
        sa.Column("value_class", sa.String(48), nullable=False),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=False),
        sa.Column("declared_priority", sa.Integer(), nullable=True),
        sa.Column("observed_use_count", sa.Integer(), nullable=True),
        sa.Column("cost_per_use", sa.Numeric(14, 2), nullable=True),
        sa.Column("duration_months", sa.Integer(), nullable=True),
        sa.Column("duplication", sa.Boolean(), nullable=True),
        sa.Column("cooling_off_regret", sa.Boolean(), nullable=True),
        sa.Column("opportunity_cost", sa.Numeric(14, 2), nullable=True),
        sa.Column("question", sa.String(300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_value_assessments_transaction_id", "value_assessments", ["transaction_id"])

    op.create_table(
        "analytics_correction_runs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("correction_version", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result_counts", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    # The v2 tables and columns contain audit evidence and user decisions that
    # have no lossless representation in the 0027 schema.  Fail inside the
    # database transaction instead of deleting production data.  A PostgreSQL
    # DO block is emitted in offline SQL too; execution aborts before Alembic
    # can move the version marker backwards.
    op.execute(
        sa.text(
            """
            DO $k_fin$
            BEGIN
                RAISE EXCEPTION USING
                    MESSAGE = 'k-fin 0028 downgrade blocked: v2 audit and accounting data cannot be restored losslessly';
            END
            $k_fin$;
            """
        )
    )
