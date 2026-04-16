"""add reports table

Revision ID: 0002_add_reports
Revises: 0001_baseline
Create Date: 2026-04-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_add_reports"
down_revision = "0002_add_agent_runs"
branch_labels = None
depends_on = None

REPORT_STATUS_ENUM = postgresql.ENUM(
    "pending", "ready", "failed", name="report_status_enum", create_type=False
)


def upgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'report_status_enum') THEN
                CREATE TYPE report_status_enum AS ENUM ('pending', 'ready', 'failed');
            END IF;
        END $$;
    """)

    op.create_table(
        "reports",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("format", sa.String(10), nullable=False),
        sa.Column("file_path", sa.String(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("status", REPORT_STATUS_ENUM, nullable=False, server_default="pending"),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_reports_period_start", "reports", ["period_start"])


def downgrade() -> None:
    op.drop_index("ix_reports_period_start", table_name="reports")
    op.drop_table("reports")
    op.execute("DROP TYPE IF EXISTS report_status_enum")
