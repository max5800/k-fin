"""refund_audit_decided_at on normalized_transactions

Marks a transaction as having been walked through the manual refund-audit
review (the "Echtes Einkommen" path on /review#refunds). Without this the
audit endpoint would re-surface the same erstattungen-Tx every page load,
because flagging a row as "real income" leaves the underlying state
unchanged (is_refund stays false, category stays erstattungen). The
timestamp lets us exclude reviewed rows without overloading is_refund.

Revision ID: 0017_refund_audit_decided
Revises: 0016_transaction_is_refund
Create Date: 2026-05-09
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0017_refund_audit_decided"
down_revision = "0016_transaction_is_refund"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "normalized_transactions",
        sa.Column(
            "refund_audit_decided_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("normalized_transactions", "refund_audit_decided_at")
