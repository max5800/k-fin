"""backfill_runs table for historical Comdirect backfill jobs

Adds a single table tracking user-initiated backfill walks. The walker
in src/scheduler/backfill.py updates this row as it progresses through
month-windows; the UI polls it for progress display.

Revision ID: 0015_backfill_runs
Revises: 0014_agent_run_reliability
Create Date: 2026-05-01
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0015_backfill_runs"
down_revision = "0014_agent_run_reliability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create the enum type idempotently. We can't rely on SQLAlchemy's
    # checkfirst alone — when the column also references the enum,
    # create_table issues a second CREATE TYPE without IF NOT EXISTS.
    # Wrap in DO/EXCEPTION so a stale type from a prior failed run is fine.
    op.execute(
        "DO $$ BEGIN "
        "CREATE TYPE backfillstatus AS ENUM ('running', 'succeeded', 'failed', 'cancelled'); "
        "EXCEPTION WHEN duplicate_object THEN null; "
        "END $$;"
    )

    # Use create_type=False so create_table does NOT try to (re-)create
    # the type — it already exists.
    backfill_status_col = postgresql.ENUM(
        "running",
        "succeeded",
        "failed",
        "cancelled",
        name="backfillstatus",
        create_type=False,
    )

    op.create_table(
        "backfill_runs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("account_id", sa.String(), nullable=True),
        sa.Column("target_start_date", sa.Date(), nullable=False),
        sa.Column("current_window_start", sa.Date(), nullable=True),
        sa.Column("windows_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("windows_done", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_inserted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("progress_message", sa.String(), nullable=True),
        sa.Column(
            "status",
            backfill_status_col,
            nullable=False,
            server_default="running",
        ),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Lookups for "is there a running run?" — used to enforce max-1-per-user.
    op.create_index(
        "ix_backfill_runs_status_started",
        "backfill_runs",
        ["status", "started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_backfill_runs_status_started", table_name="backfill_runs")
    op.drop_table("backfill_runs")
    op.execute("DROP TYPE IF EXISTS backfillstatus")
