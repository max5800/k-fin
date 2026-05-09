"""app_settings.webhook_url — optional Discord webhook for failure notifications

Stores a single-user Discord webhook URL the worker calls best-effort when a
sync or agent run flips to FAILED. Nullable, no default — webhook is opt-in
and the absence of a value silences notifications entirely.

Revision ID: 0020_app_settings_webhook_url
Revises: 0019_instrument_price_history
Create Date: 2026-05-09
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0020_app_settings_webhook_url"
down_revision = "0019_instrument_price_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "app_settings",
        sa.Column(
            "webhook_url",
            sa.String(length=500),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("app_settings", "webhook_url")
