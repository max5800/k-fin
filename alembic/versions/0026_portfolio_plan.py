"""add portfolio savings plans and target allocations

Revision ID: 0026_portfolio_plan
Revises: 0025_app_settings_own_ibans
Create Date: 2026-06-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0026_portfolio_plan"
down_revision = "0025_app_settings_own_ibans"
branch_labels = None
depends_on = None


savings_plan_interval = postgresql.ENUM(
    "monthly",
    "quarterly",
    "yearly",
    name="savings_plan_interval",
    create_type=False,
)
portfolio_target_type = postgresql.ENUM(
    "isin",
    "bucket",
    name="portfolio_target_type",
    create_type=False,
)


def upgrade() -> None:
    savings_plan_interval.create(op.get_bind(), checkfirst=True)
    portfolio_target_type.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "savings_plans",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "isin",
            sa.String(12),
            sa.ForeignKey("instruments.isin", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="EUR"),
        sa.Column(
            "interval",
            savings_plan_interval,
            nullable=False,
            server_default="monthly",
        ),
        sa.Column("start_date", sa.Date, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("isin", name="uq_savings_plans_isin"),
    )

    op.create_table(
        "portfolio_targets",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("target_type", portfolio_target_type, nullable=False),
        sa.Column("target_key", sa.String(64), nullable=False),
        sa.Column("target_weight_pct", sa.Numeric(6, 2), nullable=False),
        sa.Column("max_weight_pct", sa.Numeric(6, 2), nullable=True),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "target_type",
            "target_key",
            name="uq_portfolio_targets_key",
        ),
    )


def downgrade() -> None:
    op.drop_table("portfolio_targets")
    op.drop_table("savings_plans")
    portfolio_target_type.drop(op.get_bind(), checkfirst=True)
    savings_plan_interval.drop(op.get_bind(), checkfirst=True)
