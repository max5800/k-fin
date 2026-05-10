"""drop unused reports.file_path column

The Report model historically supported file-backed exports (PDF/MD via
`reports_dir`) alongside the agent-produced JSON payload stored in
`Report.content`. In practice no writer ever populates `file_path` —
the orchestrator persists JSON via `content`, and the only reader is a
defensive branch in the API router that always finds `file_path is None`
and falls through to the JSON download path.

Drop the dead column. Migration is reversible: downgrade restores the
nullable column (without backfill — historical NULLs only).

Revision ID: 0021_drop_reports_file_path
Revises: 0020_app_settings_webhook_url
Create Date: 2026-05-10
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0021_drop_reports_file_path"
down_revision = "0020_app_settings_webhook_url"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("reports", "file_path")


def downgrade() -> None:
    op.add_column(
        "reports",
        sa.Column("file_path", sa.String(), nullable=True),
    )
