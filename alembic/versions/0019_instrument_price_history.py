"""instrument price history + ticker_symbol

Adds the M11 external-price-history layer:

* New ``instrument_price_history`` table — one row per (instrument, day),
  populated on demand from yfinance via the backfill-prices endpoint so
  the UI can render an instrument-level performance curve between
  Comdirect transactions.
* New ``instruments.ticker_symbol`` column — yfinance keys on tickers
  (e.g. ``SAP.DE``) but Comdirect only gives ISINs. The user sets the
  mapping manually; auto-lookup is too unreliable to rely on.

Revision ID: 0019_instrument_price_history
Revises: 0018_user_page_size_setting
Create Date: 2026-05-09
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0019_instrument_price_history"
down_revision = "0018_user_page_size_setting"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "instruments",
        sa.Column("ticker_symbol", sa.String(32), nullable=True),
    )
    op.create_index(
        "ix_instruments_ticker_symbol",
        "instruments",
        ["ticker_symbol"],
    )

    op.create_table(
        "instrument_price_history",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "isin",
            sa.String(12),
            sa.ForeignKey("instruments.isin", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("price_date", sa.Date, nullable=False, index=True),
        sa.Column("close", sa.Numeric(14, 4), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="EUR"),
        sa.Column(
            "source", sa.String(32), nullable=False, server_default="yfinance"
        ),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "isin", "price_date", name="uq_instrument_price_history_isin_date"
        ),
    )


def downgrade() -> None:
    op.drop_table("instrument_price_history")
    op.drop_index("ix_instruments_ticker_symbol", table_name="instruments")
    op.drop_column("instruments", "ticker_symbol")
