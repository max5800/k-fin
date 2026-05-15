"""external source — generalize raw/normalized identity beyond Comdirect

M16-P1 schema generalisation: introduce a `data_source` enum and a
provider-neutral `external_id` column to replace the Comdirect-only
`comdirect_id`. `sync_runs` gains a nullable `data_source` so the
run-history UI can later filter per provider.

Plan-conflict resolution: the original M16-P1 bullet asked for
`source` to enter `CANONICAL_FIELDS_FOR_HASH`, but that would change
every existing content_hash and break the FK chain (`raw_transactions`
→ `normalized_transactions` + `superseded_by`). Identity stays hash-
stable: `external_id` is a pure rename of `comdirect_id`, the hash
serializer keeps the legacy `comdirect_id` JSON key for
`source=comdirect`, and cross-source uniqueness is enforced by a
DB-level `UNIQUE(source, external_id)` partial index instead.

Forward:
- create the `data_source` enum type
- add `raw_transactions.source` (default `comdirect`, then NOT NULL)
- add `raw_transactions.external_id`, backfill from `comdirect_id`
- swap the comdirect_id index for external_id, add partial UNIQUE
- drop `raw_transactions.comdirect_id`
- mirror all of the above on `normalized_transactions`
- add `sync_runs.data_source` (nullable; existing rows are stage-only)

Reverse: drop new columns/indexes, restore `comdirect_id` from
`external_id` for the comdirect source, drop the enum type. No data
loss in either direction.

Revision ID: 0022_external_source
Revises: 0021_drop_reports_file_path
Create Date: 2026-05-11
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0022_external_source"
down_revision = "0021_drop_reports_file_path"
branch_labels = None
depends_on = None


DATA_SOURCE_VALUES = ("comdirect", "paypal", "santander_cc")


def upgrade() -> None:
    bind = op.get_bind()
    data_source = sa.Enum(*DATA_SOURCE_VALUES, name="data_source")
    data_source.create(bind, checkfirst=True)

    # raw_transactions ---------------------------------------------------
    op.add_column(
        "raw_transactions",
        sa.Column(
            "source",
            sa.Enum(*DATA_SOURCE_VALUES, name="data_source", create_type=False),
            nullable=True,
        ),
    )
    op.execute("UPDATE raw_transactions SET source = 'comdirect' WHERE source IS NULL")
    op.alter_column("raw_transactions", "source", nullable=False)

    op.add_column(
        "raw_transactions",
        sa.Column("external_id", sa.String(), nullable=True),
    )
    op.execute("UPDATE raw_transactions SET external_id = comdirect_id")

    op.drop_index("ix_raw_transactions_comdirect_id", table_name="raw_transactions")
    op.create_index(
        "ix_raw_transactions_external_id",
        "raw_transactions",
        ["external_id"],
    )
    op.create_index(
        "ix_raw_transactions_source_external_id",
        "raw_transactions",
        ["source", "external_id"],
    )
    op.drop_column("raw_transactions", "comdirect_id")

    # normalized_transactions --------------------------------------------
    op.add_column(
        "normalized_transactions",
        sa.Column(
            "source",
            sa.Enum(*DATA_SOURCE_VALUES, name="data_source", create_type=False),
            nullable=True,
        ),
    )
    op.execute(
        "UPDATE normalized_transactions SET source = 'comdirect' WHERE source IS NULL"
    )
    op.alter_column("normalized_transactions", "source", nullable=False)

    op.add_column(
        "normalized_transactions",
        sa.Column("external_id", sa.String(), nullable=True),
    )
    op.execute("UPDATE normalized_transactions SET external_id = comdirect_id")

    op.drop_index(
        "ix_normalized_transactions_comdirect_id",
        table_name="normalized_transactions",
    )
    op.create_index(
        "ix_normalized_transactions_external_id",
        "normalized_transactions",
        ["external_id"],
    )
    op.create_index(
        "ix_normalized_transactions_source_external_id",
        "normalized_transactions",
        ["source", "external_id"],
    )
    op.drop_column("normalized_transactions", "comdirect_id")

    # sync_runs ----------------------------------------------------------
    # The existing `source` column on sync_runs is semantically the
    # pipeline stage (raw_import / normalize). `data_source` is added
    # nullable so historical rows stay valid; per-provider runs going
    # forward set it explicitly.
    op.add_column(
        "sync_runs",
        sa.Column(
            "data_source",
            sa.Enum(*DATA_SOURCE_VALUES, name="data_source", create_type=False),
            nullable=True,
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()

    # sync_runs ----------------------------------------------------------
    op.drop_column("sync_runs", "data_source")

    # normalized_transactions --------------------------------------------
    op.add_column(
        "normalized_transactions",
        sa.Column("comdirect_id", sa.String(), nullable=True),
    )
    op.execute(
        "UPDATE normalized_transactions SET comdirect_id = external_id "
        "WHERE source = 'comdirect'"
    )
    op.drop_index(
        "ix_normalized_transactions_source_external_id",
        table_name="normalized_transactions",
    )
    op.drop_index(
        "ix_normalized_transactions_external_id",
        table_name="normalized_transactions",
    )
    op.create_index(
        "ix_normalized_transactions_comdirect_id",
        "normalized_transactions",
        ["comdirect_id"],
    )
    op.drop_column("normalized_transactions", "external_id")
    op.drop_column("normalized_transactions", "source")

    # raw_transactions ---------------------------------------------------
    op.add_column(
        "raw_transactions",
        sa.Column("comdirect_id", sa.String(), nullable=True),
    )
    op.execute(
        "UPDATE raw_transactions SET comdirect_id = external_id "
        "WHERE source = 'comdirect'"
    )
    op.drop_index(
        "ix_raw_transactions_source_external_id",
        table_name="raw_transactions",
    )
    op.drop_index(
        "ix_raw_transactions_external_id",
        table_name="raw_transactions",
    )
    op.create_index(
        "ix_raw_transactions_comdirect_id",
        "raw_transactions",
        ["comdirect_id"],
    )
    op.drop_column("raw_transactions", "external_id")
    op.drop_column("raw_transactions", "source")

    sa.Enum(*DATA_SOURCE_VALUES, name="data_source").drop(bind, checkfirst=True)
