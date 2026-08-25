"""add durable analytics actor attribution

Revision ID: 0029_analytics_actor_attribution
Revises: 0028_trustworthy_analytics
Create Date: 2026-08-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_analytics_actor_attribution"
down_revision: str | Sequence[str] | None = "0028_trustworthy_analytics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ATTRIBUTION_DOWNGRADE_GUARD = """
DO $k_fin$
BEGIN
    IF EXISTS (
        SELECT 1 FROM source_statement_periods WHERE verified_by_user_id IS NOT NULL
    ) OR EXISTS (
        SELECT 1 FROM subscription_records WHERE owner_user_id IS NOT NULL
    ) OR EXISTS (
        SELECT 1 FROM value_assessments WHERE owner_user_id IS NOT NULL
    ) THEN
        RAISE EXCEPTION
            'k-fin 0029 downgrade blocked: analytics actor attribution contains application state';
    END IF;
END
$k_fin$;
"""


def upgrade() -> None:
    # Nullable is intentional. Legacy evidence remains readable without
    # guessing which user originally created or verified it.
    op.add_column(
        "source_statement_periods",
        sa.Column("verified_by_user_id", sa.String(36), nullable=True),
    )
    op.create_foreign_key(
        "fk_source_statement_periods_verified_by_user_id_users",
        "source_statement_periods",
        "users",
        ["verified_by_user_id"],
        ["id"],
    )
    op.create_index(
        "ix_source_statement_periods_verified_by_user_id",
        "source_statement_periods",
        ["verified_by_user_id"],
    )

    for table_name in ("subscription_records", "value_assessments"):
        op.add_column(
            table_name,
            sa.Column("owner_user_id", sa.String(36), nullable=True),
        )
        op.create_foreign_key(
            f"fk_{table_name}_owner_user_id_users",
            table_name,
            "users",
            ["owner_user_id"],
            ["id"],
        )
        op.create_index(
            f"ix_{table_name}_owner_user_id",
            table_name,
            ["owner_user_id"],
        )


def downgrade() -> None:
    # Never silently erase durable attribution. Empty/legacy-only databases
    # can downgrade; attributed application state must be handled explicitly.
    op.execute(sa.text(_ATTRIBUTION_DOWNGRADE_GUARD))

    for table_name in ("value_assessments", "subscription_records"):
        op.drop_index(f"ix_{table_name}_owner_user_id", table_name=table_name)
        op.drop_constraint(
            f"fk_{table_name}_owner_user_id_users",
            table_name,
            type_="foreignkey",
        )
        op.drop_column(table_name, "owner_user_id")

    op.drop_index(
        "ix_source_statement_periods_verified_by_user_id",
        table_name="source_statement_periods",
    )
    op.drop_constraint(
        "fk_source_statement_periods_verified_by_user_id_users",
        "source_statement_periods",
        type_="foreignkey",
    )
    op.drop_column("source_statement_periods", "verified_by_user_id")
