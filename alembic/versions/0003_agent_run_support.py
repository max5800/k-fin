"""Add agent_run to sync_source_enum and agent_type column to sync_runs.

Revision ID: 0003
Revises: 0002
"""

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add 'agent_run' value to the existing sync_source_enum.
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction block, but
    # Alembic wraps each migration in one.  The IF NOT EXISTS guard makes
    # this idempotent; we run it outside a transaction via autocommit.
    op.execute("ALTER TYPE sync_source_enum ADD VALUE IF NOT EXISTS 'agent_run'")

    op.add_column("sync_runs", sa.Column("agent_type", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("sync_runs", "agent_type")
    # PostgreSQL does not support removing individual enum values.
    # The 'agent_run' value stays but is harmless when unused.
