"""add token usage + cost columns to agent_runs

Revision ID: 0010_agent_run_token_usage
Revises: 0009_reviewed_suggestions
Create Date: 2026-04-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_agent_run_token_usage"
down_revision = "0009_reviewed_suggestions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("input_tokens", sa.Integer(), nullable=True))
    op.add_column("agent_runs", sa.Column("output_tokens", sa.Integer(), nullable=True))
    op.add_column("agent_runs", sa.Column("cost_usd", sa.Numeric(10, 6), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_runs", "cost_usd")
    op.drop_column("agent_runs", "output_tokens")
    op.drop_column("agent_runs", "input_tokens")
