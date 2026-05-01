"""Unit tests for the historical backfill walker.

Uses an in-memory SQLite pipeline + a mocked ComdirectClient. The
walker's interaction with the ingest pipeline is exercised via the real
``ingest_transactions`` codepath (not mocked) so dedup is also covered.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.orm import Session

from src.core.db.models import Base, BackfillRun, BackfillStatus
from src.normalization.pipeline import NormalizationPipeline
from src.scheduler.backfill import (
    INITIAL_WINDOW_DAYS,
    MAX_HALVINGS,
    count_planned_windows,
    has_running_backfill,
    mark_run_terminal,
    run_backfill,
)


@pytest.fixture
def pipeline():
    p = NormalizationPipeline("sqlite:///:memory:")
    Base.metadata.create_all(p.engine)
    return p


def _seed_run(pipeline: NormalizationPipeline, run_id: str, target: date) -> None:
    with Session(pipeline.engine) as s:
        s.add(
            BackfillRun(
                id=run_id,
                target_start_date=target,
                status=BackfillStatus.RUNNING,
            )
        )
        s.commit()


def _mock_client(window_responses: dict[tuple[str, date, date], tuple[list, int]] | list):
    """Build a mock ComdirectClient.get_transactions_window.

    ``window_responses`` can be:
    - dict keyed by (account_id, from_date, to_date) → (values, matches)
    - list of (values, matches) — returned in call order regardless of args
    """
    client = AsyncMock()
    if isinstance(window_responses, dict):

        async def _resp(account_id, from_date, to_date, *, paging_count=500):
            return window_responses.get(
                (account_id, from_date, to_date), ([], 0)
            )

        client.get_transactions_window.side_effect = _resp
    else:
        client.get_transactions_window.side_effect = list(window_responses)
    return client


def _tx(tx_id: str, booking_date: str, amount: str = "10.00") -> dict[str, Any]:
    """Build a minimal transaction dict accepted by the canonicalize pipeline.

    Mirrors the Comdirect response shape (transactionValue, not amount).
    """
    return {
        "transactionId": tx_id,
        "bookingDate": booking_date,
        "bookingStatus": "BOOKED",
        "transactionValue": {"value": amount, "unit": "EUR"},
        "remittanceInfo": f"Test {tx_id}",
    }


# ---- count_planned_windows ----------------------------------------------


def test_count_planned_windows_single_account_30_days():
    today = date(2026, 5, 1)
    target = today - timedelta(days=60)
    n = count_planned_windows(["A1"], target, today=today, initial_window_days=30)
    assert n == 2


def test_count_planned_windows_multi_account():
    today = date(2026, 5, 1)
    target = today - timedelta(days=90)
    n = count_planned_windows(["A1", "A2"], target, today=today, initial_window_days=30)
    assert n == 6


def test_count_planned_windows_partial_window_rounds_up():
    today = date(2026, 5, 1)
    target = today - timedelta(days=35)
    n = count_planned_windows(["A1"], target, today=today, initial_window_days=30)
    assert n == 2  # 35 days → 2 windows (30 + 5)


# ---- run_backfill happy path --------------------------------------------


@pytest.mark.asyncio
async def test_run_backfill_walks_one_account_and_ingests(pipeline):
    today = date(2026, 5, 1)
    target = today - timedelta(days=29)  # exactly one 30-day window
    run_id = "rid-happy"
    _seed_run(pipeline, run_id, target)

    txs = [_tx("t1", "2026-04-15"), _tx("t2", "2026-04-20")]
    client = _mock_client([(txs, 2)])

    summary = await run_backfill(
        client=client,
        pipeline=pipeline,
        run_id=run_id,
        target_start_date=target,
        account_ids=["ACC-1"],
        today=today,
    )

    assert summary.accounts_processed == 1
    assert summary.windows_done == 1
    assert summary.rows_inserted == 2

    # Run row was updated
    with Session(pipeline.engine) as s:
        row = s.get(BackfillRun, run_id)
        assert row.windows_done == 1
        assert row.rows_inserted == 2
        assert row.windows_total >= 1
        assert row.current_window_start == target


@pytest.mark.asyncio
async def test_run_backfill_skips_empty_windows_no_ingest(pipeline):
    today = date(2026, 5, 1)
    target = today - timedelta(days=59)  # two 30-day windows
    run_id = "rid-empty"
    _seed_run(pipeline, run_id, target)

    client = _mock_client([([], 0), ([], 0)])

    summary = await run_backfill(
        client=client,
        pipeline=pipeline,
        run_id=run_id,
        target_start_date=target,
        account_ids=["ACC-1"],
        today=today,
    )

    assert summary.windows_done == 2
    assert summary.rows_inserted == 0


@pytest.mark.asyncio
async def test_run_backfill_multiple_accounts(pipeline):
    today = date(2026, 5, 1)
    target = today - timedelta(days=29)
    run_id = "rid-multi"
    _seed_run(pipeline, run_id, target)

    # One window each, account A1 has 1 tx, A2 has 0
    client = _mock_client([
        ([_tx("a1-tx1", "2026-04-15")], 1),  # ACC-1
        ([], 0),                              # ACC-2
    ])

    summary = await run_backfill(
        client=client,
        pipeline=pipeline,
        run_id=run_id,
        target_start_date=target,
        account_ids=["ACC-1", "ACC-2"],
        today=today,
    )

    assert summary.accounts_processed == 2
    assert summary.windows_done == 2
    assert summary.rows_inserted == 1


# ---- cap-hit halving ----------------------------------------------------


@pytest.mark.asyncio
async def test_run_backfill_halves_window_on_cap_hit(pipeline):
    """When values >= paging_count, the walker halves the window and retries."""
    today = date(2026, 5, 1)
    target = today - timedelta(days=29)
    run_id = "rid-halve"
    _seed_run(pipeline, run_id, target)

    big_window = [_tx(f"big-{i}", "2026-04-15") for i in range(5)]  # paging_count=5 → cap
    half_window = [_tx(f"half-{i}", "2026-04-25") for i in range(2)]
    rest_window = [_tx(f"rest-{i}", "2026-04-10") for i in range(1)]

    # 1st call: full 30-day window, hits cap (5 == paging_count)
    # 2nd call: halved to 15 days, returns 2 (under cap)
    # 3rd call: next-back window, returns 1
    client = _mock_client([
        (big_window, 8),     # cap hit
        (half_window, 2),    # halved width OK
        (rest_window, 1),    # next window back
    ])

    summary = await run_backfill(
        client=client,
        pipeline=pipeline,
        run_id=run_id,
        target_start_date=target,
        account_ids=["ACC-1"],
        today=today,
        paging_count=5,
    )

    # 1 cap-hit retry + 1 successful narrow window + 1 follow-up
    assert client.get_transactions_window.await_count == 3
    assert summary.rows_inserted == 3  # 2 + 1


@pytest.mark.asyncio
async def test_run_backfill_aborts_when_halving_exhausted(pipeline):
    today = date(2026, 5, 1)
    target = today - timedelta(days=29)
    run_id = "rid-abort"
    _seed_run(pipeline, run_id, target)

    # Always return cap hit, no matter how narrow the window
    big = [_tx(f"x-{i}", "2026-04-15") for i in range(3)]
    client = _mock_client([(big, 999)] * (MAX_HALVINGS + 5))

    with pytest.raises(RuntimeError, match="still truncated"):
        await run_backfill(
            client=client,
            pipeline=pipeline,
            run_id=run_id,
            target_start_date=target,
            account_ids=["ACC-1"],
            today=today,
            paging_count=3,
        )


# ---- idempotency --------------------------------------------------------


@pytest.mark.asyncio
async def test_run_backfill_is_idempotent_on_rerun(pipeline):
    today = date(2026, 5, 1)
    target = today - timedelta(days=29)

    txs = [_tx("dup-1", "2026-04-15"), _tx("dup-2", "2026-04-20")]

    # First run
    run_a = "rid-a"
    _seed_run(pipeline, run_a, target)
    client_a = _mock_client([(txs, 2)])
    summary_a = await run_backfill(
        client=client_a, pipeline=pipeline, run_id=run_a,
        target_start_date=target, account_ids=["ACC-1"], today=today,
    )
    assert summary_a.rows_inserted == 2

    # Second run: same data — content_hash dedup means 0 new rows
    run_b = "rid-b"
    _seed_run(pipeline, run_b, target)
    client_b = _mock_client([(txs, 2)])
    summary_b = await run_backfill(
        client=client_b, pipeline=pipeline, run_id=run_b,
        target_start_date=target, account_ids=["ACC-1"], today=today,
    )
    assert summary_b.rows_inserted == 0


# ---- helpers ------------------------------------------------------------


def test_has_running_backfill_returns_false_when_none(pipeline):
    assert has_running_backfill(pipeline) is False


def test_has_running_backfill_returns_true_for_running(pipeline):
    _seed_run(pipeline, "running-row", date(2024, 1, 1))
    assert has_running_backfill(pipeline) is True


def test_has_running_backfill_ignores_terminal(pipeline):
    _seed_run(pipeline, "done-row", date(2024, 1, 1))
    mark_run_terminal(pipeline, "done-row", status=BackfillStatus.SUCCEEDED)
    assert has_running_backfill(pipeline) is False


def test_mark_run_terminal_sets_finished_at_and_error(pipeline):
    _seed_run(pipeline, "fail-row", date(2024, 1, 1))
    mark_run_terminal(pipeline, "fail-row", status=BackfillStatus.FAILED, error="boom")
    with Session(pipeline.engine) as s:
        row = s.get(BackfillRun, "fail-row")
        assert row.status == BackfillStatus.FAILED
        assert row.error == "boom"
        assert row.finished_at is not None


# ---- guardrails ---------------------------------------------------------


@pytest.mark.asyncio
async def test_run_backfill_rejects_target_in_future(pipeline):
    today = date(2026, 5, 1)
    target = today + timedelta(days=1)  # in the future — invalid
    run_id = "rid-bad-target"
    _seed_run(pipeline, run_id, target)

    client = _mock_client([])
    with pytest.raises(ValueError, match="must be before"):
        await run_backfill(
            client=client, pipeline=pipeline, run_id=run_id,
            target_start_date=target, account_ids=["ACC-1"], today=today,
        )


@pytest.mark.asyncio
async def test_run_backfill_uses_initial_window_days_from_constant(pipeline):
    """Sanity: the default is the documented 30-day window."""
    assert INITIAL_WINDOW_DAYS == 30


@pytest.mark.asyncio
async def test_run_backfill_handles_real_account_shape_amount_as_dict(pipeline):
    """Regression: Comdirect's account-transaction endpoint returns
    ``amount: {value, unit}``, not ``transactionValue``. canonicalize
    chokes on raw dicts; the walker must flatten via the Pydantic model
    before ingest.
    """
    today = date(2026, 5, 1)
    target = today - timedelta(days=29)
    run_id = "rid-real-shape"
    _seed_run(pipeline, run_id, target)

    raw = {
        "transactionId": "real-1",
        "bookingDate": "2026-04-15",
        "bookingStatus": "BOOKED",
        # Real Comdirect account shape: amount is a {value, unit} dict.
        # No transactionValue field on account transactions.
        "amount": {"value": "-12.50", "unit": "EUR"},
        "remittanceInfo": "REWE Markt",
        "creditor": {"holderName": "REWE", "iban": "DE1234567890"},
    }
    client = _mock_client([([raw], 1)])

    summary = await run_backfill(
        client=client,
        pipeline=pipeline,
        run_id=run_id,
        target_start_date=target,
        account_ids=["ACC-1"],
        today=today,
    )

    assert summary.rows_inserted == 1
