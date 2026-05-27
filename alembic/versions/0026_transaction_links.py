"""Add transaction_links for aggregate drilldown.

Revision ID: 0026_transaction_links
Revises: 0025_app_settings_own_ibans
Create Date: 2026-05-27
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0026_transaction_links"
down_revision: Union[str, None] = "0025_app_settings_own_ibans"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "transaction_links",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("parent_transaction_id", sa.String(length=64), nullable=False),
        sa.Column("child_transaction_id", sa.String(length=64), nullable=False),
        sa.Column("link_type", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["child_transaction_id"],
            ["normalized_transactions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_transaction_id"],
            ["normalized_transactions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "parent_transaction_id",
            "child_transaction_id",
            "link_type",
            name="uq_transaction_links_parent_child_type",
        ),
    )
    op.create_index(
        "ix_transaction_links_child",
        "transaction_links",
        ["child_transaction_id"],
        unique=False,
    )
    op.create_index(
        "ix_transaction_links_parent",
        "transaction_links",
        ["parent_transaction_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_transaction_links_parent", table_name="transaction_links")
    op.drop_index("ix_transaction_links_child", table_name="transaction_links")
    op.drop_table("transaction_links")
