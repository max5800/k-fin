"""Reliability tests for the categorization agent's batch loop.

We don't talk to Anthropic — every LLM call is replaced by patching
`run_in_fresh_loop` (which is what `_run_batch_once` calls to drive the
agent). That lets us simulate transient errors, persistent failures and
hangs deterministically.

Imports of `src.agents.categorization` are deferred to inside test
functions on purpose: importing it at module load captures
`src.core.config.settings`, but `tests/auth/test_jwt.py` reloads the
config module mid-suite and replaces that singleton. Lazy imports here
keep the captured settings reference current.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from pydantic_ai.exceptions import ModelAPIError
from sqlalchemy.orm import Session

from src.agents.types import CategorizationResult, CategorySuggestion
from src.core.db.models import (
    Category,
    NormalizedTransaction,
    RawTransaction,
    TypeEnum,
)


# ---------------------------------------------------------------------------
# Lazy-imported categorization module.
# ---------------------------------------------------------------------------


@pytest.fixture
def cat_mod():
    """Import categorization lazily so the captured `settings` reference is
    fresh — `tests/auth/test_jwt.py` reloads `src.core.config` mid-suite,
    which would otherwise leave a stale singleton on a module-level import."""
    from src.agents import categorization as _cat_mod

    return _cat_mod


# ---------------------------------------------------------------------------
# Seed: minimal DB with categories + 1 uncategorized transaction.
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_engine(db_engine):
    """One category + a single uncategorized transaction so the page loop
    has exactly one batch to process."""
    with Session(db_engine) as s:
        s.add(Category(id="groceries", name="Lebensmittel", type=TypeEnum.VARIABEL))
        s.add(
            RawTransaction(
                content_hash="a" * 64,
                comdirect_id="CD-1",
                raw_data={"stub": True},
            )
        )
        s.flush()
        s.add(
            NormalizedTransaction(
                id="tx-uncat-1",
                raw_content_hash="a" * 64,
                booking_date=date(2026, 4, 5),
                valuation_date=date(2026, 4, 5),
                amount=Decimal("-15.90"),
                sender="John Doe",
                recipient="Bäckerei Schmidt",
                description="Brötchen",
                category_id=None,
                is_recurring=False,
                is_outlier=False,
                internal_transfer=False,
            )
        )
        s.commit()
    return db_engine


def _fake_run_result(tx_id: str = "tx-uncat-1", confidence: float = 0.9):
    """Mimic pydantic-ai AgentRunResult: `.output` carries the model.

    Also provides empty usage so `extract_usage` returns 0/0.
    """
    output = CategorizationResult(
        suggestions=[
            CategorySuggestion(
                transaction_id=tx_id,
                suggested_category_id="groceries",
                confidence=confidence,
                reasoning="bakery",
            )
        ],
        uncategorized_count=1,
        high_confidence_count=1,
    )
    # extract_usage tries `.usage()` (newer pydantic-ai) and `.usage` attr.
    # Provide a usage object whose token attributes are zero — keeps the test
    # deterministic regardless of which internal API extract_usage uses.
    usage = SimpleNamespace(
        input_tokens=0,
        output_tokens=0,
        request_tokens=0,
        response_tokens=0,
        total_tokens=0,
    )
    return SimpleNamespace(output=output, usage=lambda: usage)


# ---------------------------------------------------------------------------
# Retry: transient error on first call → succeeds on retry.
# ---------------------------------------------------------------------------


class TestBatchRetry:
    def test_transient_error_then_success(
        self, seeded_engine, monkeypatch, cat_mod
    ):
        """ModelAPIError on first attempt → retry → succeeds. on_batch_error
        called with non-None then None (when next batch succeeds)."""

        calls: list[int] = []

        def fake_run(coro):
            # Drain the coroutine so pydantic-ai doesn't warn about un-awaited.
            try:
                coro.close()
            except Exception:
                pass
            calls.append(1)
            if len(calls) == 1:
                raise ModelAPIError(model_name="test", message="upstream blip")
            return _fake_run_result()

        monkeypatch.setattr(cat_mod, "run_in_fresh_loop", fake_run)
        # Skip the real backoff — keep the test snappy.
        monkeypatch.setattr(cat_mod.time, "sleep", lambda _s: None)

        errors_seen: list[str | None] = []

        def err_cb(msg):
            errors_seen.append(msg)

        result = cat_mod.run_categorization(
            seeded_engine, on_batch_error=err_cb, auto_apply_threshold=0.6
        )

        assert isinstance(result, CategorizationResult)
        assert len(result.suggestions) == 1
        assert len(calls) == 2  # one failure + one retry
        # The recovery path inside _run_batch swallows the transient error and
        # the eventual success clears the banner.
        # We only require that the callback was eventually cleared (None).
        assert None in errors_seen

    def test_two_consecutive_failures_propagate(
        self, seeded_engine, monkeypatch, cat_mod
    ):
        """All retries fail → error surfaces via on_batch_error and the page
        bail-out triggers (every batch failed)."""

        def fake_run(coro):
            try:
                coro.close()
            except Exception:
                pass
            raise httpx.ConnectError("DNS down")

        monkeypatch.setattr(cat_mod, "run_in_fresh_loop", fake_run)
        monkeypatch.setattr(cat_mod.time, "sleep", lambda _s: None)

        errors_seen: list[str | None] = []
        monkeypatch.setattr(cat_mod, "BATCH_MAX_RETRIES", 2)

        with pytest.raises(RuntimeError, match="fully failed"):
            cat_mod.run_categorization(
                seeded_engine,
                on_batch_error=lambda m: errors_seen.append(m),
                auto_apply_threshold=0.6,
            )

        # Callback must have been invoked at least once with a non-None
        # message describing the batch failure.
        non_none = [m for m in errors_seen if m is not None]
        assert non_none, f"expected ≥1 non-None error, got {errors_seen!r}"
        assert any("Batch" in m for m in non_none)


# ---------------------------------------------------------------------------
# Timeout: a batch hangs longer than BATCH_TIMEOUT_S.
# ---------------------------------------------------------------------------


class TestBatchTimeout:
    def test_timeout_branch_catches_future_timeout_error(
        self, seeded_engine, monkeypatch, cat_mod
    ):
        """When `fut.result(timeout=BATCH_TIMEOUT_S)` raises FutureTimeoutError,
        the loop logs it via on_batch_error, marks the batch failed, and the
        page-level `if page_succeeded == 0 and page_failed > 0` bail-out
        triggers `RuntimeError("…fully failed…")` so the run finishes
        instead of hanging.

        We can't make a real Future raise TimeoutError from inside
        `as_completed` (it only yields done futures), so we wrap the real
        executor to return a Future-like whose `.result(timeout=)` raises.
        """
        from concurrent.futures import (
            Future,
            ThreadPoolExecutor as RealPool,
            TimeoutError as FutureTimeoutError,
        )

        monkeypatch.setattr(cat_mod, "BATCH_TIMEOUT_S", 0.2)
        monkeypatch.setattr(cat_mod.time, "sleep", lambda _s: None)

        # The agent.run never gets called once we replace the executor's
        # submit, but provide a stub anyway in case anything else calls it.
        monkeypatch.setattr(
            cat_mod, "run_in_fresh_loop", lambda _c: _fake_run_result()
        )

        class _TimeoutFuture(Future):
            """Future that's `done()` (so as_completed yields it) but whose
            `.result(timeout=...)` raises FutureTimeoutError — exactly what
            the orchestrator-loop is meant to catch."""

            def __init__(self):
                super().__init__()
                # Mark as completed without a real result so as_completed
                # picks it up immediately.
                self.set_result(None)

            def result(self, timeout=None):
                raise FutureTimeoutError("simulated batch hardstop")

        class _FakePool:
            def __init__(self, *_a, **_kw):
                self._real = RealPool(max_workers=1)

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                self._real.shutdown(wait=False)
                return False

            def submit(self, *_a, **_kw):
                return _TimeoutFuture()

        monkeypatch.setattr(cat_mod, "ThreadPoolExecutor", _FakePool)

        errors_seen: list[str | None] = []
        with pytest.raises(RuntimeError, match="fully failed"):
            cat_mod.run_categorization(
                seeded_engine,
                on_batch_error=lambda m: errors_seen.append(m),
                auto_apply_threshold=0.6,
            )

        non_none = [m for m in errors_seen if m]
        assert non_none, "timeout should surface via on_batch_error"
        assert any("hardstop" in m for m in non_none), (
            f"expected 'hardstop' marker, got {non_none!r}"
        )


# ---------------------------------------------------------------------------
# Sanity: existing search_web disabled marker so categorization can run
# without a network even if SEARXNG_URL changes.
# ---------------------------------------------------------------------------


def test_module_constants_present(cat_mod):
    """Cheap smoke test: the new tunables are exported and sensible."""
    assert cat_mod.BATCH_MAX_RETRIES == 2
    assert cat_mod.BATCH_RETRY_INITIAL_BACKOFF_S == 2.0
    assert cat_mod.BATCH_TIMEOUT_S == 240


# Suppress the unused-import warning for `patch` if reorderers nudge it out.
_ = patch
