"""add per-agent usage_detail breakdown to agent_runs

Revision ID: 0011_agent_run_usage_detail
Revises: 0010_agent_run_token_usage
Create Date: 2026-04-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_agent_run_usage_detail"
down_revision = "0010_agent_run_token_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("usage_detail", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_runs", "usage_detail")
