"""rename sync_source_enum to sync_stage_enum

The `sync_runs.source` column has always held a *pipeline stage*
(`raw_import` / `normalize`), never an upstream provider. M16-P1
introduced the separate `DataSource` enum for the actual provider; this
migration finishes the enum hygiene by renaming the misleading
`sync_source_enum` Postgres type to `sync_stage_enum` to match the
renamed `SyncStage` Python enum.

Pure type rename — no column, value, or data change. Reversible.

Revision ID: 0023_rename_sync_source_to_stage
Revises: 0022_external_source
Create Date: 2026-05-15
"""

from __future__ import annotations

from alembic import op

revision = "0023_rename_sync_source_to_stage"
down_revision = "0022_external_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE sync_source_enum RENAME TO sync_stage_enum")


def downgrade() -> None:
    op.execute("ALTER TYPE sync_stage_enum RENAME TO sync_source_enum")
