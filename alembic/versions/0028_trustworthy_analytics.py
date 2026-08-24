"""version trustworthy analytics and reversible normalization state

Revision ID: 0028_trustworthy_analytics
Revises: 0027_mail_evidence_context
Create Date: 2026-08-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0028_trustworthy_analytics"
down_revision: str | Sequence[str] | None = "0027_mail_evidence_context"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_PRESERVATION_SCHEMA = "k_fin_0028_preservation"
_ROLLBACK_MARKER = "k-fin 0028 preservation snapshot is active"


_RESTORE_SQL = f"""
DO $k_fin$
DECLARE
    is_full_round_trip boolean;
    restored_in_pass integer;
    remaining integer;
    target record;
    sequence_target record;
    sequence_name text;
    sequence_value bigint;
    table_list text;
BEGIN
    IF to_regclass('{_PRESERVATION_SCHEMA}.snapshot_state') IS NOT NULL THEN
        SELECT col_description(
            'public.raw_transactions'::regclass,
            (
                SELECT attnum
                FROM pg_attribute
                WHERE attrelid = 'public.raw_transactions'::regclass
                  AND attname = 'content_hash'
                  AND NOT attisdropped
            )
        ) IS DISTINCT FROM '{_ROLLBACK_MARKER}'
        INTO is_full_round_trip;

        -- A full rollback dropped and recreated every public application
        -- table.  Remove only that newly-created, transaction-local state so
        -- deterministic seeds cannot conflict with the authoritative
        -- snapshot.  The snapshot lives in a separate schema and remains
        -- intact unless every row is restored and verified below.
        IF is_full_round_trip THEN
            SELECT string_agg(format('public.%I', tablename), ', ' ORDER BY tablename)
            INTO table_list
            FROM pg_tables
            WHERE schemaname = 'public'
              AND tablename <> 'alembic_version';

            IF table_list IS NOT NULL THEN
                EXECUTE 'TRUNCATE TABLE ' || table_list || ' RESTART IDENTITY CASCADE';
            END IF;
        ELSE
            -- A one-revision rollback kept the 0027 rows in place.  Restore
            -- only the columns owned by 0028 before exact snapshot
            -- verification; never overwrite an older-schema field that may
            -- have changed while the database was at 0027.
            UPDATE public.normalized_transactions AS current_row
            SET normalization_version = (snapshot.row_data ->> 'normalization_version')::integer,
                normalization_status = snapshot.row_data ->> 'normalization_status',
                is_active = (snapshot.row_data ->> 'is_active')::boolean,
                superseded_by_id = snapshot.row_data ->> 'superseded_by_id',
                accounting_class = snapshot.row_data ->> 'accounting_class',
                accounting_confidence =
                    (snapshot.row_data ->> 'accounting_confidence')::numeric,
                accounting_version = (snapshot.row_data ->> 'accounting_version')::integer,
                refund_verification_status =
                    snapshot.row_data ->> 'refund_verification_status'
            FROM {_PRESERVATION_SCHEMA}.snapshot_rows AS snapshot
            WHERE snapshot.table_name = 'normalized_transactions'
              AND current_row.id = snapshot.row_data ->> 'id';

            UPDATE public.transaction_links AS current_row
            SET status = snapshot.row_data ->> 'status',
                is_active = (snapshot.row_data ->> 'is_active')::boolean,
                version = (snapshot.row_data ->> 'version')::integer,
                confidence = (snapshot.row_data ->> 'confidence')::numeric,
                match_reason = snapshot.row_data ->> 'match_reason'
            FROM {_PRESERVATION_SCHEMA}.snapshot_rows AS snapshot
            WHERE snapshot.table_name = 'transaction_links'
              AND current_row.id = snapshot.row_data ->> 'id';
        END IF;

        CREATE TEMPORARY TABLE k_fin_0028_restored_tables (
            table_name text PRIMARY KEY
        ) ON COMMIT DROP;

        -- Foreign-key order is derived by retrying blocked tables.  Each
        -- failed attempt is an implicit PL/pgSQL subtransaction, so no partial
        -- insert can escape.  Self-references are checked after their single
        -- multi-row INSERT statement.
        LOOP
            restored_in_pass := 0;

            FOR target IN
                SELECT snapshot.table_name
                FROM {_PRESERVATION_SCHEMA}.snapshot_tables AS snapshot
                LEFT JOIN k_fin_0028_restored_tables AS restored
                  USING (table_name)
                WHERE restored.table_name IS NULL
                ORDER BY snapshot.table_name
            LOOP
                IF to_regclass(format('public.%I', target.table_name)) IS NULL THEN
                    RAISE EXCEPTION USING MESSAGE = format(
                        'k-fin 0028 restore blocked: public table %I is missing',
                        target.table_name
                    );
                END IF;

                BEGIN
                    EXECUTE format(
                        'INSERT INTO public.%1$I '
                        'SELECT (jsonb_populate_record(NULL::public.%1$I, row_data)).* '
                        'FROM {_PRESERVATION_SCHEMA}.snapshot_rows '
                        'WHERE table_name = %2$L '
                        'ON CONFLICT DO NOTHING',
                        target.table_name,
                        target.table_name
                    );
                    INSERT INTO k_fin_0028_restored_tables (table_name)
                    VALUES (target.table_name);
                    restored_in_pass := restored_in_pass + 1;
                EXCEPTION WHEN foreign_key_violation THEN
                    NULL;
                END;
            END LOOP;

            SELECT count(*)
            INTO remaining
            FROM {_PRESERVATION_SCHEMA}.snapshot_tables AS snapshot
            LEFT JOIN k_fin_0028_restored_tables AS restored
              USING (table_name)
            WHERE restored.table_name IS NULL;

            EXIT WHEN remaining = 0;
            IF restored_in_pass = 0 THEN
                RAISE EXCEPTION USING MESSAGE =
                    'k-fin 0028 restore blocked: unresolved foreign-key dependencies';
            END IF;
        END LOOP;

        -- ON CONFLICT is deliberately non-destructive.  A conflicting row is
        -- accepted only when its complete JSON representation is identical;
        -- otherwise the migration aborts and retains the preservation schema.
        FOR target IN
            SELECT table_name, row_count
            FROM {_PRESERVATION_SCHEMA}.snapshot_tables
            ORDER BY table_name
        LOOP
            EXECUTE format(
                'SELECT count(*) FROM {_PRESERVATION_SCHEMA}.snapshot_rows snapshot '
                'WHERE snapshot.table_name = %1$L '
                'AND EXISTS ('
                'SELECT 1 FROM public.%2$I current_row '
                'WHERE to_jsonb(current_row) = snapshot.row_data'
                ')',
                target.table_name,
                target.table_name
            ) INTO remaining;

            IF remaining <> target.row_count THEN
                RAISE EXCEPTION USING MESSAGE = format(
                    'k-fin 0028 restore blocked: table %I restored %s of %s preserved rows',
                    target.table_name,
                    remaining,
                    target.row_count
                );
            END IF;
        END LOOP;

        -- Explicit serial values do not advance recreated sequences.  Keep a
        -- higher extant value during a one-revision cycle and otherwise move
        -- each owned sequence to at least the restored maximum.
        FOR sequence_target IN
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
            ORDER BY table_name, ordinal_position
        LOOP
            sequence_name := pg_get_serial_sequence(
                format('public.%I', sequence_target.table_name),
                sequence_target.column_name
            );
            IF sequence_name IS NOT NULL THEN
                EXECUTE format(
                    'SELECT max(%I)::bigint FROM public.%I',
                    sequence_target.column_name,
                    sequence_target.table_name
                ) INTO sequence_value;
                IF sequence_value IS NOT NULL THEN
                    EXECUTE format(
                        'SELECT setval(%L, GREATEST((SELECT last_value FROM %s), %s), true)',
                        sequence_name,
                        sequence_name,
                        sequence_value
                    );
                END IF;
            END IF;
        END LOOP;

        SELECT previous_raw_hash_comment
        INTO sequence_name
        FROM {_PRESERVATION_SCHEMA}.snapshot_state
        WHERE singleton;
        EXECUTE format(
            'COMMENT ON COLUMN public.raw_transactions.content_hash IS %L',
            sequence_name
        );

        DROP SCHEMA {_PRESERVATION_SCHEMA} CASCADE;
    END IF;
END
$k_fin$;
"""


_PRESERVE_SQL = f"""
CREATE SCHEMA IF NOT EXISTS {_PRESERVATION_SCHEMA};

CREATE TABLE IF NOT EXISTS {_PRESERVATION_SCHEMA}.snapshot_state (
    singleton boolean PRIMARY KEY CHECK (singleton),
    previous_raw_hash_comment text
);

CREATE TABLE IF NOT EXISTS {_PRESERVATION_SCHEMA}.snapshot_tables (
    table_name text PRIMARY KEY,
    row_count bigint NOT NULL CHECK (row_count >= 0)
);

CREATE TABLE IF NOT EXISTS {_PRESERVATION_SCHEMA}.snapshot_rows (
    table_name text NOT NULL REFERENCES {_PRESERVATION_SCHEMA}.snapshot_tables(table_name)
        ON DELETE CASCADE,
    row_ordinal bigint NOT NULL,
    row_data jsonb NOT NULL,
    PRIMARY KEY (table_name, row_ordinal)
);

TRUNCATE TABLE
    {_PRESERVATION_SCHEMA}.snapshot_rows,
    {_PRESERVATION_SCHEMA}.snapshot_tables,
    {_PRESERVATION_SCHEMA}.snapshot_state;

INSERT INTO {_PRESERVATION_SCHEMA}.snapshot_state (
    singleton,
    previous_raw_hash_comment
)
SELECT
    true,
    col_description(
        'public.raw_transactions'::regclass,
        (
            SELECT attnum
            FROM pg_attribute
            WHERE attrelid = 'public.raw_transactions'::regclass
              AND attname = 'content_hash'
              AND NOT attisdropped
        )
    );

DO $k_fin$
DECLARE
    source_count bigint;
    snapshot_count bigint;
    target record;
BEGIN
    -- Lock every source table before reading any of them.  This produces one
    -- mutually consistent snapshot and prevents writes until the migration
    -- transaction commits or rolls back.
    FOR target IN
        SELECT tablename
        FROM pg_tables
        WHERE schemaname = 'public'
          AND tablename <> 'alembic_version'
        ORDER BY tablename
    LOOP
        EXECUTE format('LOCK TABLE public.%I IN ACCESS EXCLUSIVE MODE', target.tablename);
    END LOOP;

    FOR target IN
        SELECT tablename
        FROM pg_tables
        WHERE schemaname = 'public'
          AND tablename <> 'alembic_version'
        ORDER BY tablename
    LOOP
        EXECUTE format('SELECT count(*) FROM public.%I', target.tablename)
        INTO source_count;

        INSERT INTO {_PRESERVATION_SCHEMA}.snapshot_tables (table_name, row_count)
        VALUES (target.tablename, source_count);

        EXECUTE format(
            'INSERT INTO {_PRESERVATION_SCHEMA}.snapshot_rows '
            '(table_name, row_ordinal, row_data) '
            'SELECT %1$L, row_number() OVER (), to_jsonb(source_row) '
            'FROM public.%2$I AS source_row',
            target.tablename,
            target.tablename
        );

        SELECT count(*)
        INTO snapshot_count
        FROM {_PRESERVATION_SCHEMA}.snapshot_rows
        WHERE table_name = target.tablename;

        IF snapshot_count <> source_count THEN
            RAISE EXCEPTION USING MESSAGE = format(
                'k-fin 0028 downgrade blocked: table %I preserved %s of %s rows',
                target.tablename,
                snapshot_count,
                source_count
            );
        END IF;
    END LOOP;
END
$k_fin$;

COMMENT ON COLUMN public.raw_transactions.content_hash IS
    '{_ROLLBACK_MARKER}';
"""


def upgrade() -> None:
    op.add_column(
        "normalized_transactions",
        sa.Column("normalization_version", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "normalized_transactions",
        sa.Column("normalization_status", sa.String(32), server_default="active", nullable=False),
    )
    op.add_column(
        "normalized_transactions",
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
    )
    op.add_column(
        "normalized_transactions", sa.Column("superseded_by_id", sa.String(64), nullable=True)
    )
    op.add_column(
        "normalized_transactions",
        sa.Column(
            "accounting_class",
            sa.String(48),
            server_default="unresolved_ambiguous",
            nullable=False,
        ),
    )
    op.add_column(
        "normalized_transactions",
        sa.Column("accounting_confidence", sa.Numeric(4, 3), server_default="0.000", nullable=False),
    )
    op.add_column(
        "normalized_transactions",
        sa.Column("accounting_version", sa.Integer(), server_default="2", nullable=False),
    )
    op.add_column(
        "normalized_transactions",
        sa.Column(
            "refund_verification_status",
            sa.String(32),
            server_default="unverified",
            nullable=False,
        ),
    )
    op.create_index(
        "ix_normalized_transactions_is_active",
        "normalized_transactions",
        ["is_active"],
    )
    op.create_index(
        "ix_normalized_transactions_accounting_class",
        "normalized_transactions",
        ["accounting_class"],
    )

    op.add_column(
        "transaction_links",
        sa.Column("status", sa.String(32), server_default="active", nullable=False),
    )
    op.add_column(
        "transaction_links",
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
    )
    op.add_column(
        "transaction_links", sa.Column("version", sa.Integer(), server_default="1", nullable=False)
    )
    op.add_column(
        "transaction_links",
        sa.Column("confidence", sa.Numeric(4, 3), server_default="1.000", nullable=False),
    )
    op.add_column(
        "transaction_links", sa.Column("match_reason", sa.String(200), nullable=True)
    )
    op.create_index("ix_transaction_links_is_active", "transaction_links", ["is_active"])

    op.create_table(
        "source_statement_periods",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "source",
            postgresql.ENUM(
                "comdirect", "paypal", "santander_cc", name="data_source", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("rows_present", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("observed_row_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("verified_complete", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("verification_method", sa.String(64), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("source", "period_start", "period_end", name="uq_source_period"),
    )
    op.create_index(
        "ix_source_statement_periods_period_start", "source_statement_periods", ["period_start"]
    )

    op.create_table(
        "subscription_records",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=False),
        sa.Column("evidence_source", sa.String(32), nullable=False),
        sa.Column("transaction_id", sa.String(64), sa.ForeignKey("normalized_transactions.id"), nullable=True),
        sa.Column("amount_scenarios", sa.JSON(), nullable=True),
        sa.Column("next_review_date", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_subscription_records_status", "subscription_records", ["status"])
    op.create_index(
        "ix_subscription_records_transaction_id", "subscription_records", ["transaction_id"]
    )

    op.create_table(
        "value_assessments",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("transaction_id", sa.String(64), sa.ForeignKey("normalized_transactions.id"), nullable=False, unique=True),
        sa.Column("value_class", sa.String(48), nullable=False),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=False),
        sa.Column("declared_priority", sa.Integer(), nullable=True),
        sa.Column("observed_use_count", sa.Integer(), nullable=True),
        sa.Column("cost_per_use", sa.Numeric(14, 2), nullable=True),
        sa.Column("duration_months", sa.Integer(), nullable=True),
        sa.Column("duplication", sa.Boolean(), nullable=True),
        sa.Column("cooling_off_regret", sa.Boolean(), nullable=True),
        sa.Column("opportunity_cost", sa.Numeric(14, 2), nullable=True),
        sa.Column("question", sa.String(300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_value_assessments_transaction_id", "value_assessments", ["transaction_id"])

    op.create_table(
        "analytics_correction_runs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("correction_version", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result_counts", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )

    # A prior downgrade keeps a lossless JSONB snapshot outside the public
    # migration graph.  Restore only after the complete 0028 schema exists;
    # any mismatch aborts transactionally and leaves the snapshot available.
    op.execute(sa.text(_RESTORE_SQL))


def downgrade() -> None:
    # Public v2 objects have no representation at 0027, and a full downgrade
    # also removes the raw audit ledger.  Preserve every application table as
    # JSONB under an enum-independent schema before removing any public object.
    # PostgreSQL transactional DDL makes snapshot + reverse schema changes
    # atomic; the subsequent 0028 upgrade restores and verifies exact rows.
    op.execute(sa.text(_PRESERVE_SQL))

    op.drop_table("analytics_correction_runs")

    op.drop_index("ix_value_assessments_transaction_id", table_name="value_assessments")
    op.drop_table("value_assessments")

    op.drop_index("ix_subscription_records_transaction_id", table_name="subscription_records")
    op.drop_index("ix_subscription_records_status", table_name="subscription_records")
    op.drop_table("subscription_records")

    op.drop_index(
        "ix_source_statement_periods_period_start",
        table_name="source_statement_periods",
    )
    op.drop_table("source_statement_periods")

    op.drop_index("ix_transaction_links_is_active", table_name="transaction_links")
    op.drop_column("transaction_links", "match_reason")
    op.drop_column("transaction_links", "confidence")
    op.drop_column("transaction_links", "version")
    op.drop_column("transaction_links", "is_active")
    op.drop_column("transaction_links", "status")

    op.drop_index(
        "ix_normalized_transactions_accounting_class",
        table_name="normalized_transactions",
    )
    op.drop_index(
        "ix_normalized_transactions_is_active",
        table_name="normalized_transactions",
    )
    op.drop_column("normalized_transactions", "refund_verification_status")
    op.drop_column("normalized_transactions", "accounting_version")
    op.drop_column("normalized_transactions", "accounting_confidence")
    op.drop_column("normalized_transactions", "accounting_class")
    op.drop_column("normalized_transactions", "superseded_by_id")
    op.drop_column("normalized_transactions", "is_active")
    op.drop_column("normalized_transactions", "normalization_status")
    op.drop_column("normalized_transactions", "normalization_version")
