"""add app_settings singleton table

Revision ID: 0007_app_settings
Revises: 0006_add_budgets
Create Date: 2026-04-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_app_settings"
down_revision = "0006_add_budgets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "auto_apply_confidence",
            sa.Numeric(3, 2),
            nullable=False,
            server_default="0.60",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # Seed singleton row
    op.execute("INSERT INTO app_settings (id, auto_apply_confidence) VALUES (1, 0.60)")


def downgrade() -> None:
    op.drop_table("app_settings")
