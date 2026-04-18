"""add reviewed_suggestions table for low-confidence reject tracking

Revision ID: 0009_reviewed_suggestions
Revises: 0008_agent_run_progress
Create Date: 2026-04-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_reviewed_suggestions"
down_revision = "0008_agent_run_progress"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reviewed_suggestions",
        sa.Column("transaction_id", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["transaction_id"], ["normalized_transactions.id"]),
        sa.PrimaryKeyConstraint("transaction_id"),
    )


def downgrade() -> None:
    op.drop_table("reviewed_suggestions")
