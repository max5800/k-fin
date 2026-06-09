"""Shared deterministic context helpers for analysis agents."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from src.services import financial_aggregates

logger = logging.getLogger(__name__)


def get_safe_analysis_context(
    engine: Engine | None,
    *,
    year: int,
    month: int,
) -> dict[str, Any]:
    """Return analysis context when a real DB engine is available.

    Unit tests often exercise agents with ``engine=None`` or ``MagicMock`` to
    verify prompt/date behavior without a DB. Production should get the full
    deterministic context, while unavailable context is represented explicitly
    instead of aborting the whole agent run.
    """

    if not isinstance(engine, Engine):
        return {
            "available": False,
            "reason": "engine_unavailable",
            "year": year,
            "month": month,
        }

    try:
        with Session(engine) as session:
            return financial_aggregates.analysis_context(
                session,
                year=year,
                month=month,
            )
    except Exception as exc:  # noqa: BLE001 - analysis should degrade, not crash.
        logger.warning("analysis_context unavailable for %04d-%02d: %s", year, month, exc)
        return {
            "available": False,
            "reason": type(exc).__name__,
            "year": year,
            "month": month,
        }
