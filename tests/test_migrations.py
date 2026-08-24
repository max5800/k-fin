"""Forward and production-safe migration tests.

The CI `test-migrations` job only runs `upgrade head` once. These tests
add real coverage for the preservation-backed 0028 boundary and reversible
earlier migrations, including the 0022 `external_id` backfill and restoration.

Each test runs against its own throw-away PostgreSQL container so the
schema mutations never leak into the shared integration-test database.
"""

from __future__ import annotations

import contextlib
import io
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
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


_PRESERVED_TABLES = (
    "raw_transactions",
    "normalized_transactions",
    "transaction_links",
    "mail_evidence",
    "transaction_evidence_links",
    "source_statement_periods",
    "subscription_records",
    "value_assessments",
    "analytics_correction_runs",
)


def _json_snapshots(engine) -> dict[str, list[str]]:
    snapshots: dict[str, list[str]] = {}
    with engine.connect() as conn:
        for table_name in _PRESERVED_TABLES:
            snapshots[table_name] = list(
                conn.execute(
                    text(
                        f"SELECT to_jsonb(snapshot_row)::text "
                        f"FROM (SELECT * FROM {table_name}) AS snapshot_row "
                        "ORDER BY 1"
                    )
                ).scalars()
            )
    return snapshots


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


def test_0028_round_trips_populated_evidence_and_fails_closed(fresh_db_url):
    """Partial and full rollback cycles preserve every source/evidence row."""
    from alembic import command

    cfg = _alembic_config()
    with _force_db_url(fresh_db_url):
        command.upgrade(cfg, "head")

    engine = create_engine(fresh_db_url)
    try:
        _seed_0028_evidence(engine)
        expected = _json_snapshots(engine)

        # One-revision rollback: public raw and 0027 evidence rows stay exact;
        # only 0028-owned public objects are removed after the snapshot commits.
        with _force_db_url(fresh_db_url):
            command.downgrade(cfg, "0027_mail_evidence_context")

        tables = set(inspect(engine).get_table_names())
        assert "analytics_correction_runs" not in tables
        assert "source_statement_periods" not in tables
        assert "raw_transactions" in tables
        columns = {
            column["name"]
            for column in inspect(engine).get_columns("normalized_transactions")
        }
        assert "accounting_class" not in columns
        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT count(*) FROM raw_transactions")
            ).scalar_one() == 2
            assert conn.execute(
                text(
                    "SELECT row_count FROM k_fin_0028_preservation.snapshot_tables "
                    "WHERE table_name = 'raw_transactions'"
                )
            ).scalar_one() == 2
            assert conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "0027_mail_evidence_context"

        # A conflicting 0027-era write is never overwritten.  Re-upgrade
        # fails transactionally, keeps that write, and retains the snapshot.
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE normalized_transactions SET description = 'changed-at-0027' "
                    "WHERE id = :first_hash"
                ),
                {"first_hash": "a" * 64},
            )
        with _force_db_url(fresh_db_url), pytest.raises(
            DBAPIError, match="restore blocked"
        ):
            command.upgrade(cfg, "head")
        with engine.connect() as conn:
            assert conn.execute(
                text(
                    "SELECT description FROM normalized_transactions WHERE id = :first_hash"
                ),
                {"first_hash": "a" * 64},
            ).scalar_one() == "changed-at-0027"
            assert conn.execute(
                text("SELECT to_regclass('k_fin_0028_preservation.snapshot_rows')")
            ).scalar_one() == "k_fin_0028_preservation.snapshot_rows"
            assert conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "0027_mail_evidence_context"

        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE normalized_transactions SET description = 'preserved-before' "
                    "WHERE id = :first_hash"
                ),
                {"first_hash": "a" * 64},
            )
        with _force_db_url(fresh_db_url):
            command.upgrade(cfg, "head")
        assert _json_snapshots(engine) == expected

        # Full repository smoke contract: all public tables may disappear at
        # base, but raw source records and every v2/evidence row remain in the
        # enum-independent preservation schema and return exactly on upgrade.
        with _force_db_url(fresh_db_url):
            command.downgrade(cfg, "base")
        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT to_regclass('public.raw_transactions')")
            ).scalar_one() is None
            assert conn.execute(
                text(
                    "SELECT row_count FROM k_fin_0028_preservation.snapshot_tables "
                    "WHERE table_name = 'raw_transactions'"
                )
            ).scalar_one() == 2

        with _force_db_url(fresh_db_url):
            command.upgrade(cfg, "head")
        assert _json_snapshots(engine) == expected
        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT to_regclass('k_fin_0028_preservation.snapshot_rows')")
            ).scalar_one() is None
            assert conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "0028_trustworthy_analytics"
    finally:
        engine.dispose()


def test_0028_offline_sql_preserves_before_reverse_and_restores_exactly():
    """Offline artifacts preserve first and retain fail-closed verification."""
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
    preserve_position = downgrade_sql.index("CREATE SCHEMA IF NOT EXISTS")
    first_public_drop = downgrade_sql.index("DROP TABLE analytics_correction_runs")
    assert preserve_position < first_public_drop
    assert "LOCK TABLE public.%I IN ACCESS EXCLUSIVE MODE" in downgrade_sql
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
