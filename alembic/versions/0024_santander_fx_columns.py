"""add FX columns to normalized_transactions (M16-P2c)

Santander credit-card transactions are routinely foreign-currency purchases
(a travel card — the common case, not the exception). The settlement
``amount`` / ``currency`` already capture the EUR leg; these two nullable
columns preserve the *original* foreign leg so the UI can show
"19.99 USD @ 1.08" in the transaction detail instead of a blind EUR cast.

Both columns are NULL for every Comdirect and PayPal row and for any
Santander transaction that settled in EUR. They sit *outside* the ten
``CANONICAL_FIELDS_FOR_HASH`` — content hashes stay byte-identical.

Revision ID: 0024_santander_fx_columns
Revises: 0023_rename_sync_source_to_stage
Create Date: 2026-05-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024_santander_fx_columns"
down_revision = "0023_rename_sync_source_to_stage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "normalized_transactions",
        sa.Column("original_amount", sa.Numeric(14, 2), nullable=True),
    )
    op.add_column(
        "normalized_transactions",
        sa.Column("original_currency", sa.String(length=3), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("normalized_transactions", "original_currency")
    op.drop_column("normalized_transactions", "original_amount")
