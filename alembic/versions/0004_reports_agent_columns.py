"""add report_type and content columns to reports

Revision ID: 0004_reports_agent_columns
Revises: 0003_add_reports
Create Date: 2026-04-16
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0004_reports_agent_columns"
down_revision = "0003_add_reports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reports",
        sa.Column("report_type", sa.String(), nullable=True),
    )
    op.add_column(
        "reports",
        sa.Column("content", sa.JSON(), nullable=True),
    )
    # file_path was NOT NULL but agent reports don't have files
    op.alter_column("reports", "file_path", nullable=True)
    op.create_index("ix_reports_report_type", "reports", ["report_type"])


def downgrade() -> None:
    op.drop_index("ix_reports_report_type", table_name="reports")
    op.drop_column("reports", "content")
    op.drop_column("reports", "report_type")
    op.alter_column("reports", "file_path", nullable=False)
