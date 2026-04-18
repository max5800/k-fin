"""add progress columns to agent_runs

Revision ID: 0008_agent_run_progress
Revises: 0007_app_settings
Create Date: 2026-04-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_agent_run_progress"
down_revision = "0007_app_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("progress_current", sa.Integer(), nullable=True))
    op.add_column("agent_runs", sa.Column("progress_total", sa.Integer(), nullable=True))
    op.add_column("agent_runs", sa.Column("progress_message", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_runs", "progress_message")
    op.drop_column("agent_runs", "progress_total")
    op.drop_column("agent_runs", "progress_current")
