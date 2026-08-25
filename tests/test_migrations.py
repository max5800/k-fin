"""Forward and production-safe migration tests.

The CI `test-migrations` job only runs `upgrade head` once. These tests
add real coverage for the preservation-backed 0028 boundary and reversible
earlier migrations, including the 0022 `external_id` backfill and restoration.

Each test runs against its own throw-away PostgreSQL database, provisioned
either from an explicit test-only admin URL or from testcontainers.
"""

from __future__ import annotations

import contextlib
import concurrent.futures
import io
import os
import sys
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

_ROOT = Path(__file__).resolve().parent.parent


def _docker_available() -> bool:
    import shutil
    import subprocess

    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "info"], capture_output=True, timeout=5
            ).returncode
            == 0
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


@contextlib.contextmanager
def _force_db_url(url: str):
    """Point *every* `Settings` instance at `url` for the duration.

    alembic's `env.py` resolves the target DB from `settings.database_url`.
    The codebase carries more than one `Settings` instance, and other
    integration tests mutate them globally without restoring — so a
    single `monkeypatch.setattr` is not enough to guarantee alembic hits
    *this* test's container. Patch them all, restore them all.
    """
    from src.core.config import Settings

    patched: list[tuple[object, str]] = []
    seen: set[int] = set()
    for module in list(sys.modules.values()):
        candidate = getattr(module, "settings", None)
        if isinstance(candidate, Settings) and id(candidate) not in seen:
            seen.add(id(candidate))
            patched.append((candidate, candidate.database_url))
            candidate.database_url = url
    try:
        yield
    finally:
        for instance, original in patched:
            instance.database_url = original


@pytest.fixture
def fresh_db_url():
    """A brand-new empty PostgreSQL database — isolated per test."""
    external_admin_url = os.environ.get("KFIN_TEST_POSTGRES_ADMIN_URL")
    if external_admin_url:
        database_name = f"kfin_migration_{uuid.uuid4().hex}"
        admin_engine = create_engine(external_admin_url, isolation_level="AUTOCOMMIT")
        database_url = make_url(external_admin_url).set(database=database_name)
        try:
            with admin_engine.connect() as conn:
                conn.execute(text(f'CREATE DATABASE "{database_name}"'))
            yield database_url.render_as_string(hide_password=False)
        finally:
            with admin_engine.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'))
            admin_engine.dispose()
        return

    if not _docker_available():
        pytest.skip("Docker not available — migration tests need testcontainers")

    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine", driver="psycopg") as pg:
        url = pg.get_connection_url()
        engine = create_engine(url)
        for _ in range(30):
            try:
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                break
            except Exception:
                time.sleep(0.3)
        engine.dispose()
        yield url


def _alembic_config():
    from alembic.config import Config

    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    return cfg


def _database_state(engine) -> dict[str, dict[str, object]]:
    """Snapshot every public table and complete sequence definition/state."""
    tables: dict[str, object] = {}
    sequences: dict[str, object] = {}
    with engine.connect() as conn:
        table_names = conn.execute(
            text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' ORDER BY tablename"
            )
        ).scalars()
        for table_name in table_names:
            quoted_table = conn.dialect.identifier_preparer.quote(table_name)
            tables[table_name] = {
                "owner": conn.execute(
                    text(
                        "SELECT table_relation.relowner::regrole::text "
                        "FROM pg_class AS table_relation "
                        "WHERE table_relation.oid = to_regclass(:qualified_name)"
                    ),
                    {"qualified_name": f"public.{table_name}"},
                ).scalar_one(),
                "rows": list(
                    conn.execute(
                        text(
                            "SELECT to_jsonb(snapshot_row)::text "
                            f"FROM (SELECT * FROM {quoted_table}) AS snapshot_row "
                            "ORDER BY 1"
                        )
                    ).scalars()
                ),
            }

        sequence_names = conn.execute(
            text(
                "SELECT sequencename FROM pg_sequences "
                "WHERE schemaname = 'public' ORDER BY sequencename"
            )
        ).scalars()
        for sequence_name in sequence_names:
            quoted_sequence = conn.dialect.identifier_preparer.quote(sequence_name)
            state = tuple(
                conn.execute(text(f"SELECT last_value, is_called FROM {quoted_sequence}")).one()
            )
            definition = tuple(
                conn.execute(
                    text(
                        "SELECT sequence_metadata.seqtypid::regtype::text, "
                        "sequence_metadata.seqstart, sequence_metadata.seqincrement, "
                        "sequence_metadata.seqmin, sequence_metadata.seqmax, "
                        "sequence_metadata.seqcache, sequence_metadata.seqcycle, "
                        "sequence_relation.relpersistence, "
                        "sequence_relation.relowner::regrole::text, "
                        "sequence_relation.relacl::text, sequence_relation.reloptions::text, "
                        "obj_description(sequence_relation.oid, 'pg_class'), "
                        "owner_namespace.nspname, owner_relation.relname, "
                        "owner_attribute.attname, dependency.deptype, "
                        "owner_attribute.attidentity, "
                        "pg_get_expr(owner_default.adbin, owner_default.adrelid) "
                        "FROM pg_class AS sequence_relation "
                        "JOIN pg_sequence AS sequence_metadata "
                        "  ON sequence_metadata.seqrelid = sequence_relation.oid "
                        "LEFT JOIN pg_depend AS dependency "
                        "  ON dependency.classid = 'pg_class'::regclass "
                        " AND dependency.objid = sequence_relation.oid "
                        " AND dependency.deptype IN ('a', 'i') "
                        "LEFT JOIN pg_class AS owner_relation "
                        "  ON owner_relation.oid = dependency.refobjid "
                        "LEFT JOIN pg_namespace AS owner_namespace "
                        "  ON owner_namespace.oid = owner_relation.relnamespace "
                        "LEFT JOIN pg_attribute AS owner_attribute "
                        "  ON owner_attribute.attrelid = owner_relation.oid "
                        " AND owner_attribute.attnum = dependency.refobjsubid "
                        "LEFT JOIN pg_attrdef AS owner_default "
                        "  ON owner_default.adrelid = owner_relation.oid "
                        " AND owner_default.adnum = owner_attribute.attnum "
                        "WHERE sequence_relation.oid = "
                        "to_regclass(:qualified_name)"
                    ),
                    {"qualified_name": f"public.{sequence_name}"},
                ).one()
            )
            sequences[sequence_name] = {"state": state, "definition": definition}
    return {"tables": tables, "sequences": sequences}


def _seed_0028_evidence(engine) -> None:
    first_hash = "a" * 64
    second_hash = "b" * 64
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO raw_transactions "
                "(content_hash, source, external_id, raw_data, version, superseded_by) "
                "VALUES "
                "(:first_hash, 'comdirect', 'DUMMY-OLD', '{\"stub\": true}', 1, :second_hash), "
                "(:second_hash, 'comdirect', 'DUMMY-NEW', '{\"stub\": true}', 2, NULL)"
            ),
            {"first_hash": first_hash, "second_hash": second_hash},
        )
        conn.execute(
            text(
                "INSERT INTO normalized_transactions "
                "(id, raw_content_hash, source, external_id, booking_date, valuation_date, "
                " amount, currency, is_recurring, is_outlier, internal_transfer, is_refund, "
                " normalization_version, normalization_status, is_active, superseded_by_id, "
                " accounting_class, accounting_confidence, accounting_version, "
                " refund_verification_status, description) "
                "VALUES "
                "(:first_hash, :first_hash, 'comdirect', 'DUMMY-OLD', '2026-01-01', "
                " '2026-01-01', 0.00, 'EUR', false, false, true, false, 1, 'superseded', "
                " false, :second_hash, 'excluded_internal_transfer', 0.875, 2, 'verified', "
                " 'preserved-before'), "
                "(:second_hash, :second_hash, 'comdirect', 'DUMMY-NEW', '2026-01-02', "
                " '2026-01-02', 0.00, 'EUR', false, false, false, false, 2, 'active', true, "
                " NULL, 'unresolved_ambiguous', 0.000, 2, 'unverified', 'preserved-second')"
            ),
            {"first_hash": first_hash, "second_hash": second_hash},
        )
        conn.execute(
            text(
                "INSERT INTO transaction_links "
                "(id, parent_transaction_id, child_transaction_id, link_type, status, "
                " is_active, version, confidence, match_reason) "
                "VALUES ('dummy-link', :first_hash, :second_hash, 'funding', 'superseded', "
                " false, 2, 0.500, 'synthetic evidence')"
            ),
            {"first_hash": first_hash, "second_hash": second_hash},
        )
        conn.execute(
            text(
                "INSERT INTO mail_evidence "
                "(id, source, evidence_type, merchant_name, currency, confidence) "
                "VALUES ('dummy-mail', 'fixture', 'receipt', 'Example Merchant', 'EUR', 0.500)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO transaction_evidence_links "
                "(id, transaction_id, evidence_id, match_type, confidence, match_reason) "
                "VALUES ('dummy-evidence-link', :first_hash, 'dummy-mail', 'manual', 0.500, "
                "'synthetic evidence')"
            ),
            {"first_hash": first_hash},
        )
        conn.execute(
            text(
                "INSERT INTO source_statement_periods "
                "(id, source, period_start, period_end, rows_present, observed_row_count, "
                " verified_complete, verification_method) "
                "VALUES ('dummy-period', 'comdirect', '2026-01-01', '2026-01-31', true, 2, "
                " false, 'synthetic')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO subscription_records "
                "(id, label, status, confidence, evidence_source, transaction_id, "
                " amount_scenarios) "
                "VALUES ('dummy-subscription', 'Example Subscription', 'review', 0.500, "
                " 'manual', :first_hash, '[{\"amount\": \"0.00\"}]')"
            ),
            {"first_hash": first_hash},
        )
        conn.execute(
            text(
                "INSERT INTO value_assessments "
                "(id, transaction_id, value_class, confidence, question) "
                "VALUES ('dummy-value', :second_hash, 'unresolved', 0.500, "
                " 'Synthetic review question?')"
            ),
            {"second_hash": second_hash},
        )
        conn.execute(
            text(
                "INSERT INTO analytics_correction_runs "
                "(id, correction_version, mode, status, result_counts) "
                "VALUES ('dummy-audit-run', 1, 'apply', 'succeeded', "
                " '{\"unchanged\": 2}')"
            )
        )


def test_0028_populated_rule_downgrade_fails_before_any_mutation(fresh_db_url):
    """A valid rule targeting a seeded category blocks the entire downgrade."""
    from alembic import command

    cfg = _alembic_config()
    with _force_db_url(fresh_db_url):
        command.upgrade(cfg, "head")

    engine = create_engine(fresh_db_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO rules (regex_pattern, target_category_id, priority) "
                    "VALUES ('synthetic-streaming', 'abos-streaming', 10)"
                )
            )
        expected = _database_state(engine)

        with _force_db_url(fresh_db_url), pytest.raises(
            DBAPIError, match="table rules contains application state"
        ):
            command.downgrade(cfg, "base")

        assert _database_state(engine) == expected
        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "0029_analytics_actor_attribution"
            assert conn.execute(
                text("SELECT to_regnamespace('k_fin_0028_preservation')")
            ).scalar_one() is None
            assert conn.execute(
                text(
                    "SELECT count(*) FROM rules "
                    "WHERE target_category_id = 'abos-streaming'"
                )
            ).scalar_one() == 1
    finally:
        engine.dispose()


def test_0028_populated_evidence_downgrade_rolls_back_exactly(fresh_db_url):
    """All tables, rows, sequences, schema, and revision stay byte-for-byte stable."""
    from alembic import command

    cfg = _alembic_config()
    with _force_db_url(fresh_db_url):
        command.upgrade(cfg, "head")

    engine = create_engine(fresh_db_url)
    try:
        _seed_0028_evidence(engine)
        expected = _database_state(engine)

        with _force_db_url(fresh_db_url), pytest.raises(
            DBAPIError, match="contains application state"
        ):
            command.downgrade(cfg, "0027_mail_evidence_context")

        assert _database_state(engine) == expected
        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "0029_analytics_actor_attribution"
            assert conn.execute(
                text("SELECT to_regnamespace('k_fin_0028_preservation')")
            ).scalar_one() is None
    finally:
        engine.dispose()


def test_0028_customized_bootstrap_category_fails_closed(fresh_db_url):
    """Every category field, not only its seed id, belongs to application state."""
    from alembic import command

    cfg = _alembic_config()
    with _force_db_url(fresh_db_url):
        command.upgrade(cfg, "head")

    engine = create_engine(fresh_db_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE categories SET description = 'Synthetic operator note' "
                    "WHERE id = 'abos-streaming'"
                )
            )
        expected = _database_state(engine)

        with _force_db_url(fresh_db_url), pytest.raises(
            DBAPIError, match="table categories contains application state"
        ):
            command.downgrade(cfg, "base")

        assert _database_state(engine) == expected
    finally:
        engine.dispose()


def test_0028_consumed_serial_with_empty_tables_fails_closed(fresh_db_url):
    """Deleted rows cannot hide application state retained by a serial sequence."""
    from alembic import command

    cfg = _alembic_config()
    with _force_db_url(fresh_db_url):
        command.upgrade(cfg, "head")

    engine = create_engine(fresh_db_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO rules (regex_pattern, target_category_id, priority) "
                    "VALUES ('synthetic-streaming', 'abos-streaming', 10)"
                )
            )
            conn.execute(text("DELETE FROM rules"))
        expected = _database_state(engine)

        with _force_db_url(fresh_db_url), pytest.raises(
            DBAPIError, match="serial sequence rules_id_seq contains application state"
        ):
            command.downgrade(cfg, "base")

        assert _database_state(engine) == expected
        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "0029_analytics_actor_attribution"
    finally:
        engine.dispose()


def test_0028_every_persistent_sequence_customization_fails_closed(fresh_db_url):
    """Definition, ownership, metadata, and ACL changes are all application state."""
    from alembic import command

    cfg = _alembic_config()
    with _force_db_url(fresh_db_url):
        command.upgrade(cfg, "head")

    engine = create_engine(fresh_db_url)
    customizations = (
        ("ALTER SEQUENCE rules_id_seq INCREMENT BY 2", "ALTER SEQUENCE rules_id_seq INCREMENT BY 1"),
        ("ALTER SEQUENCE rules_id_seq MINVALUE 0", "ALTER SEQUENCE rules_id_seq NO MINVALUE"),
        ("ALTER SEQUENCE rules_id_seq MAXVALUE 100000", "ALTER SEQUENCE rules_id_seq NO MAXVALUE"),
        ("ALTER SEQUENCE rules_id_seq START WITH 2", "ALTER SEQUENCE rules_id_seq START WITH 1"),
        ("ALTER SEQUENCE rules_id_seq CACHE 8", "ALTER SEQUENCE rules_id_seq CACHE 1"),
        ("ALTER SEQUENCE rules_id_seq CYCLE", "ALTER SEQUENCE rules_id_seq NO CYCLE"),
        ("ALTER SEQUENCE rules_id_seq AS bigint", "ALTER SEQUENCE rules_id_seq AS integer"),
        ("ALTER SEQUENCE rules_id_seq SET UNLOGGED", "ALTER SEQUENCE rules_id_seq SET LOGGED"),
        ("ALTER SEQUENCE rules_id_seq OWNED BY NONE", "ALTER SEQUENCE rules_id_seq OWNED BY rules.id"),
        ("COMMENT ON SEQUENCE rules_id_seq IS 'Synthetic sequence note'", "COMMENT ON SEQUENCE rules_id_seq IS NULL"),
        ("GRANT SELECT ON SEQUENCE rules_id_seq TO PUBLIC", None),
    )
    try:
        canonical = _database_state(engine)["sequences"]["rules_id_seq"]
        for customize_sql, restore_sql in customizations:
            with engine.begin() as conn:
                conn.execute(text(customize_sql))
            expected = _database_state(engine)

            with _force_db_url(fresh_db_url), pytest.raises(
                DBAPIError, match="sequence .* (customized persistent definition|owned columns)"
            ):
                command.downgrade(cfg, "base")

            assert _database_state(engine) == expected
            if restore_sql is not None:
                with engine.begin() as conn:
                    conn.execute(text(restore_sql))
                assert (
                    _database_state(engine)["sequences"]["rules_id_seq"] == canonical
                ), customize_sql
    finally:
        engine.dispose()


def test_0028_coordinated_table_and_sequence_owner_change_fails_closed(
    fresh_db_url,
):
    """Equal table/sequence owners cannot conceal a changed owner identity."""
    from alembic import command

    cfg = _alembic_config()
    with _force_db_url(fresh_db_url):
        command.upgrade(cfg, "head")

    engine = create_engine(fresh_db_url)
    synthetic_owner = f"k_fin_0028_owner_{uuid.uuid4().hex}"
    quoted_owner = engine.dialect.identifier_preparer.quote(synthetic_owner)
    try:
        with engine.begin() as conn:
            conn.execute(text(f"CREATE ROLE {quoted_owner} NOLOGIN"))
            conn.execute(text(f"ALTER TABLE public.rules OWNER TO {quoted_owner}"))
            owners = tuple(
                conn.execute(
                    text(
                        "SELECT relation.relowner::regrole::text "
                        "FROM pg_class AS relation "
                        "WHERE relation.oid IN "
                        "(to_regclass('public.rules'), to_regclass('public.rules_id_seq')) "
                        "ORDER BY relation.relkind"
                    )
                ).scalars()
            )
        assert owners == (synthetic_owner, synthetic_owner)
        expected = _database_state(engine)

        with _force_db_url(fresh_db_url), pytest.raises(
            DBAPIError, match="owner verification blocked: object .* owner identity changed"
        ):
            command.downgrade(cfg, "base")

        assert _database_state(engine) == expected
        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "0029_analytics_actor_attribution"
            assert conn.execute(
                text("SELECT to_regnamespace('k_fin_0028_preservation')")
            ).scalar_one() is None
    finally:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE public.rules OWNER TO CURRENT_USER"))
            conn.execute(text(f"DROP ROLE IF EXISTS {quoted_owner}"))
        engine.dispose()


def test_0028_concurrent_sequence_definition_and_state_changes_fail_closed(
    fresh_db_url,
):
    """A downgrade waits for sequence-only writers, then sees and preserves them."""
    from alembic import command

    cfg = _alembic_config()
    with _force_db_url(fresh_db_url):
        command.upgrade(cfg, "head")

    engine = create_engine(fresh_db_url)

    def attempt_downgrade() -> None:
        command.downgrade(cfg, "base")

    try:
        for mutation_sql, failure in (
            (
                "ALTER SEQUENCE rules_id_seq CACHE 7",
                "customized persistent definition",
            ),
            ("SELECT nextval('rules_id_seq')", "contains application state"),
        ):
            writer = engine.connect()
            transaction = writer.begin()
            writer.execute(text(mutation_sql))
            writer_pid = writer.execute(text("SELECT pg_backend_pid()")).scalar_one()
            try:
                with _force_db_url(fresh_db_url), concurrent.futures.ThreadPoolExecutor(
                    max_workers=1
                ) as executor:
                    future = executor.submit(attempt_downgrade)
                    saw_lock_wait = False
                    try:
                        for _ in range(50):
                            with engine.connect() as observer:
                                saw_lock_wait = bool(
                                    observer.execute(
                                        text(
                                            "SELECT count(*) FROM pg_stat_activity "
                                            "WHERE datname = current_database() "
                                            "AND pid NOT IN (pg_backend_pid(), :writer_pid) "
                                            "AND wait_event_type = 'Lock'"
                                        ),
                                        {"writer_pid": writer_pid},
                                    ).scalar_one()
                                )
                            if saw_lock_wait:
                                break
                            time.sleep(0.1)
                    finally:
                        # Always release the writer before the executor waits
                        # for its worker, including an assertion/error path.
                        transaction.commit()
                    assert saw_lock_wait, (
                        "downgrade never waited on the sequence-only writer"
                    )
                    with pytest.raises(DBAPIError, match=failure):
                        future.result(timeout=15)
            finally:
                if transaction.is_active:
                    transaction.rollback()
                writer.close()

            expected = _database_state(engine)
            assert expected["tables"]["alembic_version"]
            sequence = expected["sequences"]["rules_id_seq"]
            if "CACHE" in mutation_sql:
                assert sequence["definition"][5] == 7
                assert sequence["state"] == (1, False)
            else:
                assert sequence["state"] == (1, True)
            with engine.connect() as conn:
                assert conn.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one() == "0029_analytics_actor_attribution"
                assert conn.execute(
                    text("SELECT to_regnamespace('k_fin_0028_preservation')")
                ).scalar_one() is None

            with engine.begin() as conn:
                conn.execute(text("ALTER SEQUENCE rules_id_seq CACHE 1 RESTART WITH 1"))
    finally:
        engine.dispose()


def test_0028_sequence_schema_move_fails_closed_and_is_preserved(fresh_db_url):
    """An owned application sequence cannot evade discovery outside public."""
    from alembic import command

    cfg = _alembic_config()
    with _force_db_url(fresh_db_url):
        command.upgrade(cfg, "head")

    engine = create_engine(fresh_db_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE SCHEMA sequence_customization"))
            conn.execute(text("ALTER SEQUENCE rules_id_seq OWNED BY NONE"))
            conn.execute(
                text(
                    "ALTER SEQUENCE rules_id_seq "
                    "SET SCHEMA sequence_customization"
                )
            )
        with engine.connect() as conn:
            expected_state = tuple(
                conn.execute(
                    text(
                        "SELECT last_value, is_called "
                        "FROM sequence_customization.rules_id_seq"
                    )
                ).one()
            )

        with _force_db_url(fresh_db_url), pytest.raises(
            DBAPIError, match="sequence lock blocked: sequence rules_id_seq has 0 owned columns"
        ):
            command.downgrade(cfg, "base")

        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "0029_analytics_actor_attribution"
            assert conn.execute(
                text("SELECT to_regclass('public.rules_id_seq')")
            ).scalar_one() is None
            assert conn.execute(
                text("SELECT to_regclass('sequence_customization.rules_id_seq')")
            ).scalar_one() == "sequence_customization.rules_id_seq"
            assert tuple(
                conn.execute(
                    text(
                        "SELECT last_value, is_called "
                        "FROM sequence_customization.rules_id_seq"
                    )
                ).one()
            ) == expected_state
            assert conn.execute(
                text("SELECT to_regnamespace('k_fin_0028_preservation')")
            ).scalar_one() is None
    finally:
        engine.dispose()


def test_0028_empty_head_base_head_smoke_is_exact(fresh_db_url):
    """Migration-owned bootstrap rows and pristine sequences may round-trip."""
    from alembic import command

    cfg = _alembic_config()
    with _force_db_url(fresh_db_url):
        command.upgrade(cfg, "head")

    engine = create_engine(fresh_db_url)
    try:
        expected = _database_state(engine)

        with _force_db_url(fresh_db_url):
            command.downgrade(cfg, "base")
        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT to_regclass('public.raw_transactions')")
            ).scalar_one() is None
            assert conn.execute(
                text("SELECT to_regclass('k_fin_0028_preservation.snapshot_rows')")
            ).scalar_one() == "k_fin_0028_preservation.snapshot_rows"

        with _force_db_url(fresh_db_url):
            command.upgrade(cfg, "head")
        assert _database_state(engine) == expected
        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT to_regclass('k_fin_0028_preservation.snapshot_rows')")
            ).scalar_one() is None
            assert conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "0029_analytics_actor_attribution"
    finally:
        engine.dispose()


def test_0028_restore_rejects_intervening_older_schema_write(fresh_db_url):
    """A write at 0027 is retained while the attempted re-upgrade rolls back."""
    from alembic import command

    cfg = _alembic_config()
    with _force_db_url(fresh_db_url):
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "0027_mail_evidence_context")

    engine = create_engine(fresh_db_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO rules (regex_pattern, target_category_id, priority) "
                    "VALUES ('synthetic-streaming', 'abos-streaming', 10)"
                )
            )

        with _force_db_url(fresh_db_url), pytest.raises(
            DBAPIError, match="restore blocked: table rules"
        ):
            command.upgrade(cfg, "head")

        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "0027_mail_evidence_context"
            assert conn.execute(text("SELECT count(*) FROM rules")).scalar_one() == 1
            assert conn.execute(
                text("SELECT to_regclass('public.analytics_correction_runs')")
            ).scalar_one() is None
            assert conn.execute(
                text("SELECT to_regclass('k_fin_0028_preservation.snapshot_rows')")
            ).scalar_one() == "k_fin_0028_preservation.snapshot_rows"
    finally:
        engine.dispose()


def test_0028_restore_rejects_and_preserves_intervening_sequence_definition(
    fresh_db_url,
):
    """An older-revision sequence-only change cannot be overwritten on restore."""
    from alembic import command

    cfg = _alembic_config()
    with _force_db_url(fresh_db_url):
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "0027_mail_evidence_context")

    engine = create_engine(fresh_db_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER SEQUENCE rules_id_seq CACHE 9"))
        expected = _database_state(engine)

        with _force_db_url(fresh_db_url), pytest.raises(
            DBAPIError, match="restore blocked: sequence rules_id_seq has a customized"
        ):
            command.upgrade(cfg, "head")

        assert _database_state(engine) == expected
        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "0027_mail_evidence_context"
            assert conn.execute(
                text("SELECT to_regclass('k_fin_0028_preservation.snapshot_rows')")
            ).scalar_one() == "k_fin_0028_preservation.snapshot_rows"
    finally:
        engine.dispose()


def test_0028_offline_sql_preserves_before_reverse_and_restores_exactly():
    """Offline artifacts guard first, then preserve eligible bootstrap state."""
    from alembic import command

    cfg = _alembic_config()
    downgrade_output = io.StringIO()
    cfg.output_buffer = downgrade_output
    with _force_db_url("postgresql+psycopg://test@localhost/test"):
        command.downgrade(
            cfg,
            "0028_trustworthy_analytics:0027_mail_evidence_context",
            sql=True,
        )
    downgrade_sql = downgrade_output.getvalue()
    guard_position = downgrade_sql.index("blocked before schema change")
    preserve_position = downgrade_sql.index("CREATE SCHEMA IF NOT EXISTS")
    first_public_drop = downgrade_sql.index("DROP TABLE analytics_correction_runs")
    assert guard_position < preserve_position < first_public_drop
    assert "LOCK TABLE public.%I IN ACCESS EXCLUSIVE MODE" in downgrade_sql
    assert "FROM pg_tables" in downgrade_sql
    assert "JOIN pg_sequence AS sequence_metadata" in downgrade_sql
    assert "sequence_metadata.seqincrement" in downgrade_sql
    assert "sequence_metadata.seqmin" in downgrade_sql
    assert "sequence_metadata.seqmax" in downgrade_sql
    assert "sequence_metadata.seqcache" in downgrade_sql
    assert "sequence_metadata.seqcycle" in downgrade_sql
    assert "ALTER SEQUENCE %I.%I OWNED BY" in downgrade_sql
    assert "owner identity changed" in downgrade_sql
    assert "serial sequence %I contains application state" in downgrade_sql
    assert "to_jsonb(source_row)" in downgrade_sql
    assert "preserved %s of %s rows" in downgrade_sql
    assert "DROP TABLE raw_transactions" not in downgrade_sql
    assert "UPDATE raw_transactions" not in downgrade_sql
    assert "DELETE FROM raw_transactions" not in downgrade_sql

    upgrade_output = io.StringIO()
    cfg.output_buffer = upgrade_output
    with _force_db_url("postgresql+psycopg://test@localhost/test"):
        command.upgrade(
            cfg,
            "0027_mail_evidence_context:0028_trustworthy_analytics",
            sql=True,
        )
    upgrade_sql = upgrade_output.getvalue()
    assert "jsonb_populate_record" in upgrade_sql
    assert "ON CONFLICT DO NOTHING" in upgrade_sql
    assert "restore blocked" in upgrade_sql
    assert upgrade_sql.index("restore blocked") < upgrade_sql.index(
        "DROP SCHEMA k_fin_0028_preservation CASCADE"
    )


def test_0029_actor_attribution_is_nullable_reversible_and_fail_closed(
    fresh_db_url,
):
    from alembic import command

    cfg = _alembic_config()
    with _force_db_url(fresh_db_url):
        command.upgrade(cfg, "head")

    engine = create_engine(fresh_db_url)
    try:
        columns = {
            table_name: {
                column["name"]: column
                for column in inspect(engine).get_columns(table_name)
            }
            for table_name in (
                "source_statement_periods",
                "subscription_records",
                "value_assessments",
            )
        }
        assert columns["source_statement_periods"]["verified_by_user_id"][
            "nullable"
        ]
        assert columns["subscription_records"]["owner_user_id"]["nullable"]
        assert columns["value_assessments"]["owner_user_id"]["nullable"]

        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users "
                    "(id, email, display_name, password_hash, is_active, role) "
                    "VALUES ('00000000-0000-0000-0000-000000000001', "
                    "'john@example.invalid', 'John Doe', 'dummy-hash', true, 'admin')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO source_statement_periods "
                    "(id, source, period_start, period_end, rows_present, "
                    "observed_row_count, verified_complete) VALUES "
                    "('legacy-period', 'comdirect', '2026-01-01', '2026-01-31', "
                    "false, 0, false)"
                )
            )
            assert conn.execute(
                text(
                    "SELECT verified_by_user_id FROM source_statement_periods "
                    "WHERE id = 'legacy-period'"
                )
            ).scalar_one() is None
            conn.execute(
                text(
                    "UPDATE source_statement_periods SET verified_by_user_id = "
                    "'00000000-0000-0000-0000-000000000001' "
                    "WHERE id = 'legacy-period'"
                )
            )

        with _force_db_url(fresh_db_url), pytest.raises(
            DBAPIError, match="actor attribution contains application state"
        ):
            command.downgrade(cfg, "0028_trustworthy_analytics")

        with engine.begin() as conn:
            assert conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "0029_analytics_actor_attribution"
            conn.execute(
                text(
                    "UPDATE source_statement_periods "
                    "SET verified_by_user_id = NULL WHERE id = 'legacy-period'"
                )
            )

        with _force_db_url(fresh_db_url):
            command.downgrade(cfg, "0028_trustworthy_analytics")
        assert "verified_by_user_id" not in {
            column["name"]
            for column in inspect(engine).get_columns("source_statement_periods")
        }

        with _force_db_url(fresh_db_url):
            command.upgrade(cfg, "head")
        with engine.connect() as conn:
            assert conn.execute(
                text(
                    "SELECT verified_by_user_id FROM source_statement_periods "
                    "WHERE id = 'legacy-period'"
                )
            ).scalar_one() is None
            assert conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "0029_analytics_actor_attribution"
    finally:
        engine.dispose()


def test_0022_backfills_external_id_and_is_reversible(fresh_db_url):
    """0022 backfills external_id from comdirect_id; downgrade restores it."""
    from alembic import command

    cfg = _alembic_config()
    engine = create_engine(fresh_db_url)

    try:
        with _force_db_url(fresh_db_url):
            # Pre-0022 state: raw_transactions still has `comdirect_id`.
            command.upgrade(cfg, "0021_drop_reports_file_path")
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO raw_transactions "
                        "(content_hash, comdirect_id, raw_data, version) "
                        "VALUES (:h, :cid, :raw, 1)"
                    ),
                    {"h": "hash-cd-1", "cid": "CD-123", "raw": "{}"},
                )
            cols_before = {
                c["name"] for c in inspect(engine).get_columns("raw_transactions")
            }
            assert "comdirect_id" in cols_before and "external_id" not in cols_before

            # Forward: 0022.
            command.upgrade(cfg, "0022_external_source")
            with engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT external_id, source FROM raw_transactions "
                        "WHERE content_hash = 'hash-cd-1'"
                    )
                ).one()
            assert row.external_id == "CD-123"
            assert row.source == "comdirect"

            insp = inspect(engine)
            cols_after = {c["name"] for c in insp.get_columns("raw_transactions")}
            assert "external_id" in cols_after and "comdirect_id" not in cols_after
            index_names = {ix["name"] for ix in insp.get_indexes("raw_transactions")}
            assert "ix_raw_transactions_source_external_id" in index_names

            # Reverse: back to 0021 restores comdirect_id from external_id.
            command.downgrade(cfg, "0021_drop_reports_file_path")
            with engine.connect() as conn:
                restored = conn.execute(
                    text(
                        "SELECT comdirect_id FROM raw_transactions "
                        "WHERE content_hash = 'hash-cd-1'"
                    )
                ).scalar_one()
            assert restored == "CD-123"
            cols_reverted = {
                c["name"] for c in inspect(engine).get_columns("raw_transactions")
            }
            assert "comdirect_id" in cols_reverted and "external_id" not in cols_reverted
    finally:
        engine.dispose()


def test_0023_renames_sync_enum_type(fresh_db_url):
    """0023 renames the Postgres enum type sync_source_enum → sync_stage_enum."""
    from alembic import command

    cfg = _alembic_config()
    engine = create_engine(fresh_db_url)

    enum_query = text("SELECT 1 FROM pg_type WHERE typname = :name")
    try:
        with _force_db_url(fresh_db_url):
            command.upgrade(cfg, "0022_external_source")
            with engine.connect() as conn:
                assert conn.execute(enum_query, {"name": "sync_source_enum"}).first()

            command.upgrade(cfg, "0023_rename_sync_source_to_stage")
            with engine.connect() as conn:
                assert conn.execute(enum_query, {"name": "sync_stage_enum"}).first()
                assert not conn.execute(
                    enum_query, {"name": "sync_source_enum"}
                ).first()

            command.downgrade(cfg, "0022_external_source")
            with engine.connect() as conn:
                assert conn.execute(enum_query, {"name": "sync_source_enum"}).first()
    finally:
        engine.dispose()
