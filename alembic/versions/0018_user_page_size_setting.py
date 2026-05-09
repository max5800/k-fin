"""app_settings.page_size — default rows-per-page for the transactions table

The UI's pagination control needs a per-deployment knob that survives
page refreshes. We piggyback on the existing AppSettings singleton row
rather than introducing a per-user table — k-fin is a single-user
deployment by design (M10 multi-user UI is deprioritised).

Revision ID: 0018_user_page_size_setting
Revises: 0017_refund_audit_decided
Create Date: 2026-05-09
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0018_user_page_size_setting"
down_revision = "0017_refund_audit_decided"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "app_settings",
        sa.Column(
            "page_size",
            sa.Integer(),
            nullable=False,
            server_default="25",
        ),
    )


def downgrade() -> None:
    op.drop_column("app_settings", "page_size")
