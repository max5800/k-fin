"""is_refund flag on normalized_transactions

A refund (Krankenkassen-Erstattung, Splitwise-Ausgleich, Spesen-Rückzahlung,
Amazon-Rückbuchung) is a positive amount that semantically *cancels* a prior
expense rather than constituting income. Without this flag, refunds inflate
the savings rate and bypass the budget for the original category.

Refund-aware aggregation: refunds are excluded from `income`, folded into
`expenses` (positive sign reduces the negative expense sum), and counted as
budget-relief on their own category.

Revision ID: 0016_transaction_is_refund
Revises: 0015_backfill_runs
Create Date: 2026-05-08
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0016_transaction_is_refund"
down_revision = "0015_backfill_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `server_default=sa.false()` stays *permanent*: the migration runs as
    # a pre-upgrade Helm hook, but old API/worker pods still serving in
    # the rollout window would otherwise hit "null value in column
    # is_refund" on every INSERT. The model-layer default already covers
    # ORM-driven inserts; the schema-level default is a rollout-safety
    # belt for direct SQL paths.
    op.add_column(
        "normalized_transactions",
        sa.Column(
            "is_refund",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    # Composite index to keep `/aggregates/budget-spending` cheap: it
    # filters by category_id + booking_date and conditionally sums by
    # is_refund, so all three columns belong on one index.
    op.create_index(
        "ix_normalized_transactions_category_refund_date",
        "normalized_transactions",
        ["category_id", "is_refund", "booking_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_normalized_transactions_category_refund_date",
        table_name="normalized_transactions",
    )
    op.drop_column("normalized_transactions", "is_refund")
