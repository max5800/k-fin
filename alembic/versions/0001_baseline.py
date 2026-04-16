"""baseline: m5 schema

Revision ID: 0001_baseline
Revises:
Create Date: 2026-04-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


# Use postgresql.ENUM with create_type=False so SQLAlchemy never emits
# CREATE TYPE implicitly during op.create_table — we create the types
# exactly once, idempotently, in upgrade() via DO blocks.
TYPE_ENUM = postgresql.ENUM("fix", "variabel", "diskretionaer", name="type_enum", create_type=False)
SYNC_STATUS_ENUM = postgresql.ENUM(
    "running", "succeeded", "failed", name="sync_status_enum", create_type=False
)
SYNC_SOURCE_ENUM = postgresql.ENUM(
    "raw_import", "normalize", name="sync_source_enum", create_type=False
)


def upgrade() -> None:
    # Idempotent ENUM creation — tolerates partial/leftover state from
    # failed previous attempts (e.g. init container CrashLoopBackOff).
    op.execute(
        """
        DO $$ BEGIN
            CREATE TYPE type_enum AS ENUM ('fix', 'variabel', 'diskretionaer');
        EXCEPTION WHEN duplicate_object THEN null; END $$;
        """
    )
    op.execute(
        """
        DO $$ BEGIN
            CREATE TYPE sync_status_enum AS ENUM ('running', 'succeeded', 'failed');
        EXCEPTION WHEN duplicate_object THEN null; END $$;
        """
    )
    op.execute(
        """
        DO $$ BEGIN
            CREATE TYPE sync_source_enum AS ENUM ('raw_import', 'normalize');
        EXCEPTION WHEN duplicate_object THEN null; END $$;
        """
    )

    op.create_table(
        "raw_transactions",
        sa.Column("content_hash", sa.String(length=64), primary_key=True),
        sa.Column("comdirect_id", sa.String(), nullable=True),
        sa.Column("raw_data", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "superseded_by",
            sa.String(length=64),
            sa.ForeignKey("raw_transactions.content_hash"),
            nullable=True,
        ),
        sa.Column("batch_id", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_raw_transactions_comdirect_id", "raw_transactions", ["comdirect_id"])
    op.create_index("ix_raw_transactions_batch_id", "raw_transactions", ["batch_id"])

    op.create_table(
        "categories",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False, unique=True),
        sa.Column("type", TYPE_ENUM, nullable=False),
    )

    op.create_table(
        "rules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("regex_pattern", sa.String(), nullable=False),
        sa.Column(
            "target_category_id",
            sa.String(),
            sa.ForeignKey("categories.id"),
            nullable=False,
        ),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
    )

    op.create_table(
        "recurring_patterns",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("recipient", sa.String(), nullable=False),
        sa.Column("avg_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("amount_stddev", sa.Numeric(14, 2), nullable=False),
        sa.Column("first_seen_month", sa.Date(), nullable=False),
        sa.Column("last_seen_month", sa.Date(), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False),
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
    op.create_index("ix_recurring_patterns_recipient", "recurring_patterns", ["recipient"])

    op.create_table(
        "normalized_transactions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "raw_content_hash",
            sa.String(length=64),
            sa.ForeignKey("raw_transactions.content_hash"),
            nullable=False,
        ),
        sa.Column("comdirect_id", sa.String(), nullable=True),
        sa.Column("booking_date", sa.Date(), nullable=False),
        sa.Column("valuation_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="EUR"),
        sa.Column("sender", sa.String(), nullable=True),
        sa.Column("recipient", sa.String(), nullable=True),
        sa.Column("sender_iban", sa.String(), nullable=True),
        sa.Column("recipient_iban", sa.String(), nullable=True),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("category_id", sa.String(), sa.ForeignKey("categories.id"), nullable=True),
        sa.Column("is_recurring", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_outlier", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("internal_transfer", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "recurring_pattern_id",
            sa.Integer(),
            sa.ForeignKey("recurring_patterns.id"),
            nullable=True,
        ),
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
    op.create_index(
        "ix_normalized_transactions_raw_content_hash",
        "normalized_transactions",
        ["raw_content_hash"],
    )
    op.create_index(
        "ix_normalized_transactions_comdirect_id",
        "normalized_transactions",
        ["comdirect_id"],
    )
    op.create_index(
        "ix_normalized_transactions_booking_date",
        "normalized_transactions",
        ["booking_date"],
    )

    op.create_table(
        "tags",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False, unique=True),
    )

    op.create_table(
        "transaction_tags",
        sa.Column(
            "transaction_id",
            sa.String(),
            sa.ForeignKey("normalized_transactions.id"),
            primary_key=True,
        ),
        sa.Column("tag_id", sa.String(), sa.ForeignKey("tags.id"), primary_key=True),
    )

    op.create_table(
        "sync_runs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("source", SYNC_SOURCE_ENUM, nullable=False),
        sa.Column("status", SYNC_STATUS_ENUM, nullable=False, server_default="running"),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rows_processed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("sync_runs")
    op.drop_table("transaction_tags")
    op.drop_table("tags")
    op.drop_index("ix_normalized_transactions_booking_date", table_name="normalized_transactions")
    op.drop_index("ix_normalized_transactions_comdirect_id", table_name="normalized_transactions")
    op.drop_index(
        "ix_normalized_transactions_raw_content_hash",
        table_name="normalized_transactions",
    )
    op.drop_table("normalized_transactions")
    op.drop_index("ix_recurring_patterns_recipient", table_name="recurring_patterns")
    op.drop_table("recurring_patterns")
    op.drop_table("rules")
    op.drop_table("categories")
    op.drop_index("ix_raw_transactions_batch_id", table_name="raw_transactions")
    op.drop_index("ix_raw_transactions_comdirect_id", table_name="raw_transactions")
    op.drop_table("raw_transactions")

    bind = op.get_bind()
    SYNC_SOURCE_ENUM.drop(bind, checkfirst=True)
    SYNC_STATUS_ENUM.drop(bind, checkfirst=True)
    TYPE_ENUM.drop(bind, checkfirst=True)
