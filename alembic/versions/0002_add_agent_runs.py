"""add agent_runs table

Revision ID: 0002_add_agent_runs
Revises: 0001_baseline
Create Date: 2026-04-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_add_agent_runs"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create enums idempotently
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'runstatus') THEN
                CREATE TYPE runstatus AS ENUM ('pending', 'running', 'succeeded', 'failed');
            END IF;
        END $$;
    """)
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'runtrigger') THEN
                CREATE TYPE runtrigger AS ENUM ('manual', 'scheduled', 'webhook');
            END IF;
        END $$;
    """)

    op.create_table(
        "agent_runs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("agent_name", sa.String(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "running", "succeeded", "failed", name="runstatus", create_type=False),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "trigger",
            sa.Enum("manual", "scheduled", "webhook", name="runtrigger", create_type=False),
            nullable=False,
            server_default="manual",
        ),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_runs_agent_name", "agent_runs", ["agent_name"])


def downgrade() -> None:
    op.drop_index("ix_agent_runs_agent_name", table_name="agent_runs")
    op.drop_table("agent_runs")
    op.execute("DROP TYPE IF EXISTS runstatus")
    op.execute("DROP TYPE IF EXISTS runtrigger")
