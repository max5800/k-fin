"""add portfolio tables (depots, instruments, positions, depot_transactions, portfolio_snapshots)

Revision ID: 0012_add_portfolio
Revises: 0011_agent_run_usage_detail
Create Date: 2026-04-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_add_portfolio"
down_revision = "0011_agent_run_usage_detail"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "depots",
        sa.Column("depot_id", sa.String(64), primary_key=True),
        sa.Column("client_id", sa.String, nullable=True),
        sa.Column("depot_type", sa.String, nullable=True),
        sa.Column("currency", sa.String(3), nullable=False, server_default="EUR"),
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
    )

    op.create_table(
        "instruments",
        sa.Column("isin", sa.String(12), primary_key=True),
        sa.Column("wkn", sa.String(12), nullable=True, index=True),
        sa.Column("name", sa.String, nullable=False, server_default=""),
        sa.Column("instrument_type", sa.String(32), nullable=True, index=True),
        sa.Column("currency", sa.String(3), nullable=False, server_default="EUR"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "positions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "depot_id",
            sa.String(64),
            sa.ForeignKey("depots.depot_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "isin",
            sa.String(12),
            sa.ForeignKey("instruments.isin", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("current_price", sa.Numeric(14, 4), nullable=False, server_default="0"),
        sa.Column("current_value", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("purchase_value", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("prev_day_price", sa.Numeric(14, 4), nullable=True),
        sa.Column("prev_day_value", sa.Numeric(14, 2), nullable=True),
        sa.Column("currency", sa.String(3), nullable=False, server_default="EUR"),
        sa.Column(
            "as_of",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("depot_id", "isin", name="uq_positions_depot_isin"),
    )

    op.create_table(
        "depot_transactions",
        sa.Column("transaction_id", sa.String(64), primary_key=True),
        sa.Column(
            "depot_id",
            sa.String(64),
            sa.ForeignKey("depots.depot_id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "isin",
            sa.String(12),
            sa.ForeignKey("instruments.isin", ondelete="RESTRICT"),
            nullable=True,
            index=True,
        ),
        sa.Column("booking_date", sa.Date, nullable=False, index=True),
        sa.Column("transaction_type", sa.String(16), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 6), nullable=False, server_default="0"),
        sa.Column("price", sa.Numeric(14, 4), nullable=False, server_default="0"),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(3), nullable=False, server_default="EUR"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "portfolio_snapshots",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "depot_id",
            sa.String(64),
            sa.ForeignKey("depots.depot_id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
        sa.Column("snapshot_date", sa.Date, nullable=False, index=True),
        sa.Column("total_value", sa.Numeric(14, 2), nullable=False),
        sa.Column("total_purchase_value", sa.Numeric(14, 2), nullable=False),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "depot_id", "snapshot_date", name="uq_portfolio_snapshots_depot_date"
        ),
    )


def downgrade() -> None:
    op.drop_table("portfolio_snapshots")
    op.drop_table("depot_transactions")
    op.drop_table("positions")
    op.drop_table("instruments")
    op.drop_table("depots")
