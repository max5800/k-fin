"""agent_runs reliability columns + cancelled status

Adds heartbeat_at and last_error to agent_runs, and extends the
runstatus enum with 'cancelled'. Required by the M11 worker-resident
run executor + stale-run reaper + user-cancel endpoint.

Revision ID: 0014_agent_run_reliability
Revises: 0013_users
Create Date: 2026-05-01
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0014_agent_run_reliability"
down_revision = "0013_users"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "agent_runs",
        sa.Column("last_error", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_agent_runs_status_heartbeat",
        "agent_runs",
        ["status", "heartbeat_at"],
    )
    # Postgres enum types must be ALTERed outside a transaction.
    op.execute("COMMIT")
    op.execute("ALTER TYPE runstatus ADD VALUE IF NOT EXISTS 'cancelled'")


def downgrade() -> None:
    op.drop_index("ix_agent_runs_status_heartbeat", table_name="agent_runs")
    op.drop_column("agent_runs", "last_error")
    op.drop_column("agent_runs", "heartbeat_at")
    # Postgres has no DROP VALUE on enums; leaving 'cancelled' in place is safe.
