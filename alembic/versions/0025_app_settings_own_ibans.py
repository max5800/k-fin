"""add own_ibans to app_settings — UI-editable internal-transfer IBAN list

The normalization pipeline flags a debit+credit between two of the user's
own accounts as an internal transfer (kept out of income/expense). The list
of own-account IBANs used to be the ``OWN_IBANS`` env var; it is now a
UI-editable setting on the singleton ``app_settings`` row, so the maintainer
enters the IBANs on the Settings page instead of a git-ignored Helm overlay.
The env var is removed in the same change.

``server_default=""`` backfills the existing singleton row.

Revision ID: 0025_app_settings_own_ibans
Revises: 0024_santander_fx_columns
Create Date: 2026-05-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025_app_settings_own_ibans"
down_revision = "0024_santander_fx_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "app_settings",
        sa.Column(
            "own_ibans",
            sa.String(length=500),
            nullable=False,
            server_default="",
        ),
    )


def downgrade() -> None:
    op.drop_column("app_settings", "own_ibans")
