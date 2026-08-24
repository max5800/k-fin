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


_EMPTY_CATEGORY_ROWS = (
    ("miete", "Miete & Nebenkosten", "fix"),
    ("strom-gas", "Strom & Gas", "fix"),
    ("internet-telefon", "Internet & Telefon", "fix"),
    ("versicherungen", "Versicherungen", "fix"),
    ("auto-fix", "Auto (Steuer, Versicherung)", "fix"),
    ("oepnv-abo", "ÖPNV-Abo", "fix"),
    ("abos-streaming", "Abos & Streaming", "fix"),
    ("mitgliedschaften", "Mitgliedschaften", "fix"),
    ("kredit-tilgung", "Kredit & Tilgung", "fix"),
    ("gez", "Rundfunkbeitrag", "fix"),
    ("lebensmittel", "Lebensmittel", "variabel"),
    ("drogerie", "Drogerie & Hygiene", "variabel"),
    ("tanken", "Tanken & Laden", "variabel"),
    ("auto-variabel", "Auto (Werkstatt, Parken)", "variabel"),
    ("gesundheit", "Gesundheit & Apotheke", "variabel"),
    ("haushalt", "Haushalt & Wohnung", "variabel"),
    ("restaurant-cafe", "Restaurant & Café", "diskretionaer"),
    ("bars-ausgehen", "Bars & Ausgehen", "diskretionaer"),
    ("kleidung", "Kleidung & Schuhe", "diskretionaer"),
    ("elektronik", "Elektronik & Software", "diskretionaer"),
    ("freizeit", "Freizeit & Hobby", "diskretionaer"),
    ("reisen", "Reisen & Urlaub", "diskretionaer"),
    ("geschenke", "Geschenke & Spenden", "diskretionaer"),
    ("bildung", "Bildung & Bücher", "diskretionaer"),
    ("gehalt", "Gehalt & Lohn", "fix"),
    ("nebeneinkuenfte", "Nebeneinkünfte", "variabel"),
    ("kapitalertraege", "Kapitalerträge", "variabel"),
    ("erstattungen", "Erstattungen & Rückzahlungen", "variabel"),
    ("etf-sparplan", "ETF-Sparplan", "fix"),
    ("wertpapierkauf", "Wertpapierkauf", "diskretionaer"),
    ("umbuchung", "Umbuchung", "fix"),
    ("gebuehren-zinsen", "Gebühren & Zinsen", "fix"),
)


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


_EMPTY_CATEGORY_VALUES_SQL = ",\n            ".join(
    "(" + ", ".join(_sql_literal(value) for value in row) + ")"
    for row in _EMPTY_CATEGORY_ROWS
)


def _snapshot_guard_sql(only_with_snapshot: bool) -> str:
    return (
        f"IF to_regclass('{_PRESERVATION_SCHEMA}.snapshot_state') IS NULL THEN "
        "RETURN; END IF;"
        if only_with_snapshot
        else ""
    )


def _lock_application_tables_sql(*, only_with_snapshot: bool) -> str:
    return f"""
DO $k_fin$
DECLARE
    target record;
BEGIN
    {_snapshot_guard_sql(only_with_snapshot)}

    FOR target IN
        SELECT tablename
        FROM pg_tables
        WHERE schemaname = 'public'
          AND tablename <> 'alembic_version'
        ORDER BY tablename
    LOOP
        EXECUTE format('LOCK TABLE public.%I IN ACCESS EXCLUSIVE MODE', target.tablename);
    END LOOP;
END
$k_fin$;
"""


def _lock_application_sequences_sql(*, only_with_snapshot: bool) -> str:
    return f"""
DO $k_fin$
DECLARE
    dependency_type text;
    identity_mode text;
    owner_column_name text;
    owner_dependency_count integer;
    owner_schema_name text;
    owner_table_name text;
    sequence_target record;
BEGIN
    {_snapshot_guard_sql(only_with_snapshot)}

    -- The preceding Alembic statement already holds every owner table.  This
    -- statement therefore discovers stable ownership in a fresh snapshot.
    -- Reasserting the existing serial ownership (or identity generation mode)
    -- is a semantic no-op that takes the sequence's ShareRowExclusiveLock,
    -- blocking nextval/setval and sequence DDL until the transaction ends.
    FOR sequence_target IN
        SELECT sequence_relation.oid AS sequence_oid,
               sequence_namespace.nspname AS schema_name,
               sequence_relation.relname AS sequence_name
        FROM pg_class AS sequence_relation
        JOIN pg_namespace AS sequence_namespace
          ON sequence_namespace.oid = sequence_relation.relnamespace
        WHERE sequence_relation.relkind = 'S'
          AND (
              sequence_namespace.nspname = 'public'
              OR EXISTS (
                  SELECT 1
                  FROM pg_depend AS owned_dependency
                  JOIN pg_class AS owned_relation
                    ON owned_relation.oid = owned_dependency.refobjid
                  JOIN pg_namespace AS owned_namespace
                    ON owned_namespace.oid = owned_relation.relnamespace
                  WHERE owned_dependency.classid = 'pg_class'::regclass
                    AND owned_dependency.objid = sequence_relation.oid
                    AND owned_dependency.deptype IN ('a', 'i')
                    AND owned_namespace.nspname = 'public'
                    AND owned_relation.relname <> 'alembic_version'
              )
              OR EXISTS (
                  SELECT 1
                  FROM pg_depend AS default_dependency
                  JOIN pg_attrdef AS referenced_default
                    ON default_dependency.classid = 'pg_attrdef'::regclass
                   AND referenced_default.oid = default_dependency.objid
                  JOIN pg_class AS default_relation
                    ON default_relation.oid = referenced_default.adrelid
                  JOIN pg_namespace AS default_namespace
                    ON default_namespace.oid = default_relation.relnamespace
                  WHERE default_dependency.refclassid = 'pg_class'::regclass
                    AND default_dependency.refobjid = sequence_relation.oid
                    AND default_namespace.nspname = 'public'
                    AND default_relation.relname <> 'alembic_version'
              )
          )
        ORDER BY sequence_relation.relname
    LOOP
        SELECT count(*),
               min(owner_namespace.nspname),
               min(owner_relation.relname),
               min(owner_attribute.attname),
               min(dependency.deptype::text),
               min(owner_attribute.attidentity::text)
        INTO owner_dependency_count,
             owner_schema_name,
             owner_table_name,
             owner_column_name,
             dependency_type,
             identity_mode
        FROM pg_depend AS dependency
        JOIN pg_class AS owner_relation
          ON owner_relation.oid = dependency.refobjid
        JOIN pg_namespace AS owner_namespace
          ON owner_namespace.oid = owner_relation.relnamespace
        JOIN pg_attribute AS owner_attribute
          ON owner_attribute.attrelid = owner_relation.oid
         AND owner_attribute.attnum = dependency.refobjsubid
         AND NOT owner_attribute.attisdropped
        WHERE dependency.classid = 'pg_class'::regclass
          AND dependency.objid = sequence_target.sequence_oid
          AND dependency.deptype IN ('a', 'i')
          AND owner_namespace.nspname = 'public'
          AND owner_relation.relname <> 'alembic_version';

        IF owner_dependency_count <> 1 THEN
            RAISE EXCEPTION USING MESSAGE = format(
                'k-fin 0028 sequence lock blocked: sequence %I has %s owned columns',
                sequence_target.sequence_name,
                owner_dependency_count
            );
        END IF;

        IF dependency_type = 'i' AND identity_mode IN ('a', 'd') THEN
            EXECUTE format(
                'ALTER TABLE %I.%I ALTER COLUMN %I SET GENERATED %s',
                owner_schema_name,
                owner_table_name,
                owner_column_name,
                CASE identity_mode WHEN 'a' THEN 'ALWAYS' ELSE 'BY DEFAULT' END
            );
        ELSIF dependency_type = 'a' THEN
            EXECUTE format(
                'ALTER SEQUENCE %I.%I OWNED BY %I.%I.%I',
                sequence_target.schema_name,
                sequence_target.sequence_name,
                owner_schema_name,
                owner_table_name,
                owner_column_name
            );
        ELSE
            RAISE EXCEPTION USING MESSAGE = format(
                'k-fin 0028 sequence lock blocked: sequence %I has invalid ownership',
                sequence_target.sequence_name
            );
        END IF;
    END LOOP;

END
$k_fin$;
"""


_LOCK_DOWNGRADE_TABLES_SQL = _lock_application_tables_sql(only_with_snapshot=False)
_LOCK_DOWNGRADE_SEQUENCES_SQL = _lock_application_sequences_sql(
    only_with_snapshot=False
)
_LOCK_RESTORE_TABLES_SQL = _lock_application_tables_sql(only_with_snapshot=True)
_LOCK_RESTORE_SEQUENCES_SQL = _lock_application_sequences_sql(
    only_with_snapshot=True
)


_FAIL_CLOSED_SQL = f"""
DO $k_fin$
DECLARE
    unexpected_rows bigint;
    target record;
    sequence_target record;
    sequence_value bigint;
    sequence_is_called boolean;
BEGIN
    -- Hold every application table stable from the predicate through the
    -- reverse migration.  A blocked attempt releases these locks when its
    -- transaction aborts and has not changed any row, sequence, or schema.
    FOR target IN
        SELECT tablename
        FROM pg_tables
        WHERE schemaname = 'public'
          AND tablename <> 'alembic_version'
        ORDER BY tablename
    LOOP
        EXECUTE format('LOCK TABLE public.%I IN ACCESS EXCLUSIVE MODE', target.tablename);
    END LOOP;

    -- Inspect every public application table dynamically.  Only the exact
    -- migration-owned category catalog and app-settings singleton constitute
    -- an empty database; changes to either are application state too.
    FOR target IN
        SELECT tablename
        FROM pg_tables
        WHERE schemaname = 'public'
          AND tablename <> 'alembic_version'
        ORDER BY tablename
    LOOP
        IF target.tablename = 'categories' THEN
            WITH seed (id, name, type) AS (
                VALUES {_EMPTY_CATEGORY_VALUES_SQL}
            ), expected AS (
                SELECT id,
                       name,
                       type,
                       'expense'::varchar AS kind,
                       true AS budgetable,
                       NULL::varchar AS analysis_group,
                       NULL::varchar AS description,
                       NULL::jsonb AS examples,
                       NULL::jsonb AS anti_examples,
                       NULL::varchar AS llm_hints
                FROM seed
            )
            SELECT count(*)
            INTO unexpected_rows
            FROM (
                (
                    SELECT id,
                           name,
                           type::text,
                           kind,
                           budgetable,
                           analysis_group,
                           description,
                           examples::jsonb,
                           anti_examples::jsonb,
                           llm_hints
                    FROM public.categories
                    EXCEPT
                    SELECT * FROM expected
                )
                UNION ALL
                (
                    SELECT * FROM expected
                    EXCEPT
                    SELECT id,
                           name,
                           type::text,
                           kind,
                           budgetable,
                           analysis_group,
                           description,
                           examples::jsonb,
                           anti_examples::jsonb,
                           llm_hints
                    FROM public.categories
                )
            ) AS differences;
        ELSIF target.tablename = 'app_settings' THEN
            WITH expected (id, auto_apply_confidence, page_size, webhook_url, own_ibans) AS (
                VALUES (1, 0.60::numeric, 25, NULL::varchar, ''::varchar)
            )
            SELECT count(*)
            INTO unexpected_rows
            FROM (
                (
                    SELECT id, auto_apply_confidence, page_size, webhook_url, own_ibans
                    FROM public.app_settings
                    EXCEPT
                    SELECT * FROM expected
                )
                UNION ALL
                (
                    SELECT * FROM expected
                    EXCEPT
                    SELECT id, auto_apply_confidence, page_size, webhook_url, own_ibans
                    FROM public.app_settings
                )
            ) AS differences;
        ELSE
            EXECUTE format('SELECT count(*) FROM public.%I', target.tablename)
            INTO unexpected_rows;
        END IF;

        IF unexpected_rows <> 0 THEN
            RAISE EXCEPTION USING MESSAGE = format(
                'k-fin 0028 downgrade blocked before schema change: '
                'table %I contains application state',
                target.tablename
            );
        END IF;
    END LOOP;

    -- Empty tables can still carry state or a customized definition in their
    -- SERIAL/IDENTITY sequence after rows were deleted.  The preceding lock
    -- statement stabilized every public sequence and the defining catalogs.
    -- Require the complete PostgreSQL CREATE SEQUENCE definition and its
    -- ownership/default relationship to be the canonical migration-created
    -- form, not merely an unconsumed last_value.
    FOR sequence_target IN
        SELECT sequence_namespace.nspname AS schema_name,
               sequence_relation.relname AS sequence_name,
               sequence_relation.relname = left(
                   owner_relation.relname || '_' || owner_attribute.attname || '_seq',
                   63
               ) AS has_canonical_name,
               sequence_namespace.nspname = owner_namespace.nspname
                   AS has_canonical_schema,
               sequence_relation.relowner = owner_relation.relowner AS has_canonical_owner,
               sequence_relation.relpersistence = owner_relation.relpersistence
                   AS has_canonical_persistence,
               sequence_relation.relacl IS NULL AS has_canonical_acl,
               sequence_relation.reloptions IS NULL AS has_canonical_reloptions,
               obj_description(sequence_relation.oid, 'pg_class') IS NULL
                   AS has_no_comment,
               NOT EXISTS (
                   SELECT 1 FROM pg_seclabel
                   WHERE pg_seclabel.classoid = 'pg_class'::regclass
                     AND pg_seclabel.objoid = sequence_relation.oid
               ) AS has_no_security_label,
               sequence_metadata.seqtypid = owner_attribute.atttypid
                   AS has_canonical_type,
               sequence_metadata.seqtypid AS sequence_type_oid,
               sequence_metadata.seqstart AS start_value,
               sequence_metadata.seqincrement AS increment_value,
               sequence_metadata.seqmin AS minimum_value,
               sequence_metadata.seqmax AS maximum_value,
               sequence_metadata.seqcache AS cache_value,
               sequence_metadata.seqcycle AS cycles,
               dependency.deptype,
               owner_attribute.attidentity,
               pg_get_expr(owner_default.adbin, owner_default.adrelid)
                   AS owner_default_expression,
               format('%I.%I', sequence_namespace.nspname, sequence_relation.relname)::regclass::text
                   AS sequence_regclass_text
        FROM pg_class AS sequence_relation
        JOIN pg_namespace AS sequence_namespace
          ON sequence_namespace.oid = sequence_relation.relnamespace
        JOIN pg_sequence AS sequence_metadata
          ON sequence_metadata.seqrelid = sequence_relation.oid
        JOIN pg_depend AS dependency
          ON dependency.classid = 'pg_class'::regclass
         AND dependency.objid = sequence_relation.oid
         AND dependency.deptype IN ('a', 'i')
        JOIN pg_class AS owner_relation
          ON owner_relation.oid = dependency.refobjid
        JOIN pg_namespace AS owner_namespace
          ON owner_namespace.oid = owner_relation.relnamespace
        JOIN pg_attribute AS owner_attribute
          ON owner_attribute.attrelid = owner_relation.oid
         AND owner_attribute.attnum = dependency.refobjsubid
         AND NOT owner_attribute.attisdropped
        LEFT JOIN pg_attrdef AS owner_default
          ON owner_default.adrelid = owner_relation.oid
         AND owner_default.adnum = owner_attribute.attnum
        WHERE sequence_relation.relkind = 'S'
          AND owner_namespace.nspname = 'public'
          AND owner_relation.relname <> 'alembic_version'
        ORDER BY sequence_relation.relname
    LOOP
        IF NOT sequence_target.has_canonical_name
           OR NOT sequence_target.has_canonical_schema
           OR NOT sequence_target.has_canonical_owner
           OR NOT sequence_target.has_canonical_persistence
           OR NOT sequence_target.has_canonical_acl
           OR NOT sequence_target.has_canonical_reloptions
           OR NOT sequence_target.has_no_comment
           OR NOT sequence_target.has_no_security_label
           OR NOT sequence_target.has_canonical_type
           OR sequence_target.start_value <> 1
           OR sequence_target.increment_value <> 1
           OR sequence_target.minimum_value <> 1
           OR sequence_target.maximum_value <> (CASE sequence_target.sequence_type_oid
               WHEN 'smallint'::regtype THEN 32767
               WHEN 'integer'::regtype THEN 2147483647
               WHEN 'bigint'::regtype THEN 9223372036854775807
               ELSE -1
           END)
           OR sequence_target.cache_value <> 1
           OR sequence_target.cycles
           OR (
               sequence_target.deptype = 'i'
               AND sequence_target.attidentity NOT IN ('a', 'd')
           )
           OR (
               sequence_target.deptype = 'a'
               AND (
                   sequence_target.attidentity <> ''
                   OR sequence_target.owner_default_expression IS DISTINCT FROM
                      format('nextval(%L::regclass)', sequence_target.sequence_regclass_text)
               )
           )
        THEN
            RAISE EXCEPTION USING MESSAGE = format(
                'k-fin 0028 downgrade blocked before schema change: '
                'sequence %I has a customized persistent definition',
                sequence_target.sequence_name
            );
        END IF;

        EXECUTE format(
            'SELECT last_value, is_called FROM %I.%I',
            sequence_target.schema_name,
            sequence_target.sequence_name
        )
        INTO sequence_value, sequence_is_called;

        IF sequence_value <> sequence_target.start_value OR sequence_is_called THEN
            RAISE EXCEPTION USING MESSAGE = format(
                'k-fin 0028 downgrade blocked before schema change: '
                'serial sequence %I contains application state',
                sequence_target.sequence_name
            );
        END IF;
    END LOOP;
END
$k_fin$;
"""


_RESTORE_SQL = f"""
DO $k_fin$
DECLARE
    is_full_round_trip boolean;
    restored_in_pass integer;
    remaining integer;
    current_count bigint;
    target record;
    sequence_target record;
    sequence_name text;
    sequence_value bigint;
    sequence_is_called boolean;
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

            EXECUTE format(
                'SELECT count(*) FROM public.%I',
                target.table_name
            ) INTO current_count;

            IF remaining <> target.row_count OR current_count <> target.row_count THEN
                RAISE EXCEPTION USING MESSAGE = format(
                    'k-fin 0028 restore blocked: table %I restored %s of %s '
                    'preserved rows with %s current rows',
                    target.table_name,
                    remaining,
                    target.row_count,
                    current_count
                );
            END IF;
        END LOOP;

        -- The downgrade guard admitted only pristine owned sequences and seed
        -- rows use explicit ids.  Verify that no sequence was consumed while
        -- parked at an older revision; never overwrite such intervening state.
        FOR sequence_target IN
            SELECT sequence_namespace.nspname AS schema_name,
                   sequence_relation.relname AS sequence_name,
                   sequence_relation.relname = left(
                       owner_relation.relname || '_' || owner_attribute.attname || '_seq',
                       63
                   ) AS has_canonical_name,
                   sequence_namespace.nspname = owner_namespace.nspname
                       AS has_canonical_schema,
                   sequence_relation.relowner = owner_relation.relowner
                       AS has_canonical_owner,
                   sequence_relation.relpersistence = owner_relation.relpersistence
                       AS has_canonical_persistence,
                   sequence_relation.relacl IS NULL AS has_canonical_acl,
                   sequence_relation.reloptions IS NULL AS has_canonical_reloptions,
                   obj_description(sequence_relation.oid, 'pg_class') IS NULL
                       AS has_no_comment,
                   NOT EXISTS (
                       SELECT 1 FROM pg_seclabel
                       WHERE pg_seclabel.classoid = 'pg_class'::regclass
                         AND pg_seclabel.objoid = sequence_relation.oid
                   ) AS has_no_security_label,
                   sequence_metadata.seqtypid = owner_attribute.atttypid
                       AS has_canonical_type,
                   sequence_metadata.seqtypid AS sequence_type_oid,
                   sequence_metadata.seqstart AS start_value,
                   sequence_metadata.seqincrement AS increment_value,
                   sequence_metadata.seqmin AS minimum_value,
                   sequence_metadata.seqmax AS maximum_value,
                   sequence_metadata.seqcache AS cache_value,
                   sequence_metadata.seqcycle AS cycles,
                   dependency.deptype,
                   owner_attribute.attidentity,
                   pg_get_expr(owner_default.adbin, owner_default.adrelid)
                       AS owner_default_expression,
                   format(
                       '%I.%I', sequence_namespace.nspname, sequence_relation.relname
                   )::regclass::text AS sequence_regclass_text
            FROM pg_class AS sequence_relation
            JOIN pg_namespace AS sequence_namespace
              ON sequence_namespace.oid = sequence_relation.relnamespace
            JOIN pg_sequence AS sequence_metadata
              ON sequence_metadata.seqrelid = sequence_relation.oid
            JOIN pg_depend AS dependency
              ON dependency.classid = 'pg_class'::regclass
             AND dependency.objid = sequence_relation.oid
             AND dependency.deptype IN ('a', 'i')
            JOIN pg_class AS owner_relation
              ON owner_relation.oid = dependency.refobjid
            JOIN pg_namespace AS owner_namespace
              ON owner_namespace.oid = owner_relation.relnamespace
            JOIN pg_attribute AS owner_attribute
              ON owner_attribute.attrelid = owner_relation.oid
             AND owner_attribute.attnum = dependency.refobjsubid
             AND NOT owner_attribute.attisdropped
            LEFT JOIN pg_attrdef AS owner_default
              ON owner_default.adrelid = owner_relation.oid
             AND owner_default.adnum = owner_attribute.attnum
            WHERE sequence_relation.relkind = 'S'
              AND owner_namespace.nspname = 'public'
              AND owner_relation.relname <> 'alembic_version'
            ORDER BY sequence_relation.relname
        LOOP
            IF NOT sequence_target.has_canonical_name
               OR NOT sequence_target.has_canonical_schema
               OR NOT sequence_target.has_canonical_owner
               OR NOT sequence_target.has_canonical_persistence
               OR NOT sequence_target.has_canonical_acl
               OR NOT sequence_target.has_canonical_reloptions
               OR NOT sequence_target.has_no_comment
               OR NOT sequence_target.has_no_security_label
               OR NOT sequence_target.has_canonical_type
               OR sequence_target.start_value <> 1
               OR sequence_target.increment_value <> 1
               OR sequence_target.minimum_value <> 1
               OR sequence_target.maximum_value <> (CASE sequence_target.sequence_type_oid
                   WHEN 'smallint'::regtype THEN 32767
                   WHEN 'integer'::regtype THEN 2147483647
                   WHEN 'bigint'::regtype THEN 9223372036854775807
                   ELSE -1
               END)
               OR sequence_target.cache_value <> 1
               OR sequence_target.cycles
               OR (
                   sequence_target.deptype = 'i'
                   AND sequence_target.attidentity NOT IN ('a', 'd')
               )
               OR (
                   sequence_target.deptype = 'a'
                   AND (
                       sequence_target.attidentity <> ''
                       OR sequence_target.owner_default_expression IS DISTINCT FROM
                          format(
                              'nextval(%L::regclass)',
                              sequence_target.sequence_regclass_text
                          )
                   )
               )
            THEN
                RAISE EXCEPTION USING MESSAGE = format(
                    'k-fin 0028 restore blocked: sequence %I has a customized '
                    'persistent definition while the preservation snapshot was active',
                    sequence_target.sequence_name
                );
            END IF;

            EXECUTE format(
                'SELECT last_value, is_called FROM %I.%I',
                sequence_target.schema_name,
                sequence_target.sequence_name
            )
            INTO sequence_value, sequence_is_called;

            IF sequence_value <> sequence_target.start_value OR sequence_is_called THEN
                RAISE EXCEPTION USING MESSAGE = format(
                    'k-fin 0028 restore blocked: serial sequence %I changed '
                    'while the preservation snapshot was active',
                    sequence_target.sequence_name
                );
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
    # An explicit category makes ordinary bank/card fees classifiable without
    # guessing from free text.  Preserve an operator-defined conflicting row;
    # the downgrade guard will correctly treat it as application state.
    op.execute(
        sa.text(
            "INSERT INTO categories (id, name, type, kind, budgetable) "
            "VALUES ('gebuehren-zinsen', 'Gebühren & Zinsen', "
            "'fix'::type_enum, 'expense', true) "
            "ON CONFLICT DO NOTHING"
        )
    )
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
    op.execute(sa.text(_LOCK_RESTORE_TABLES_SQL))
    op.execute(sa.text(_LOCK_RESTORE_SEQUENCES_SQL))
    op.execute(sa.text(_RESTORE_SQL))


def downgrade() -> None:
    # No historical, evidence, audit, derived, or user-configured row has a
    # lossless representation throughout the path to base.  Refuse a
    # populated downgrade before creating the preservation schema or removing
    # any public object.  The exact migration-owned bootstrap state remains
    # eligible for the repository's empty HEAD -> base -> HEAD smoke cycle.
    op.execute(sa.text(_LOCK_DOWNGRADE_TABLES_SQL))
    op.execute(sa.text(_LOCK_DOWNGRADE_SEQUENCES_SQL))
    op.execute(sa.text(_FAIL_CLOSED_SQL))

    # Preserve the eligible bootstrap rows so their generated values also
    # round-trip exactly.  The fail-closed predicate guarantees this snapshot
    # never becomes an implicit backup mechanism for application data.
    op.execute(sa.text(_PRESERVE_SQL))

    op.execute(
        sa.text(
            "DELETE FROM categories WHERE id = 'gebuehren-zinsen' "
            "AND name = 'Gebühren & Zinsen' AND type = 'fix'::type_enum "
            "AND kind = 'expense' AND budgetable = true "
            "AND analysis_group IS NULL AND description IS NULL "
            "AND examples IS NULL AND anti_examples IS NULL AND llm_hints IS NULL"
        )
    )

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
