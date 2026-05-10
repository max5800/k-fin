"""Period-label derivation for period-aware agents.

Single entry point that turns the (agent-default-kind, optional override
days, reference date) triple into a (label, start, end) triple. Centralised
so weekly/monthly/anomaly all emit canonical, parseable labels and the
report-key collision when ``period_days`` differs from the natural window
goes away.

Two label formats only:
  - default semantics (no override): "YYYY-Www" (weekly), "YYYY-MM"
    (monthly), or "YYYY-MM-DD/YYYY-MM-DD" (anomaly)
  - override semantics (``days`` set): always
    "YYYY-MM-DD/YYYY-MM-DD" — the form change *is* the override signal,
    so the orchestrator can persist the report under a distinct key
    instead of clobbering the regular weekly/monthly slot.

Note on override-window arithmetic: the existing per-agent conventions
are preserved. Weekly/monthly treat ``days`` as N-day inclusive
(``start = ref - (days - 1)``), anomaly treats it as a 30-day rolling
lookback (``start = ref - days``, matching the pre-existing
``lookback_days`` semantics that callers in the API layer rely on).
The centralisation is about emitting one canonical label format, not
about retroactively unifying off-by-one quirks.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Literal

PeriodKind = Literal["weekly", "monthly", "anomaly"]


def derive_period_label(
    default_kind: PeriodKind,
    days: int | None,
    ref: date,
) -> tuple[str, date, date]:
    """Return ``(label, start, end)`` for an agent run.

    When ``days`` is None the function uses each agent's natural window:

    * ``weekly``  — current ISO week (Mon..Sun) of ``ref``,
      label ``"YYYY-Www"``.
    * ``monthly`` — previous complete calendar month of ``ref``,
      label ``"YYYY-MM"``.
    * ``anomaly`` — 30-day lookback ending at ``ref`` (i.e.
      ``[ref - 30d, ref]``), label ``"YYYY-MM-DD/YYYY-MM-DD"``.

    When ``days`` is a positive int the window becomes a rolling range
    ending at ``ref`` and the label switches to
    ``"YYYY-MM-DD/YYYY-MM-DD"`` for *every* agent. The label switch is
    intentional: persisting an override run under e.g. ``"2026-W19"``
    would collide with the regular weekly slot whenever ``days != 7``.

    Override window arithmetic preserves the pre-existing per-agent
    conventions (see module docstring).
    """
    if days is not None and days > 0:
        if default_kind == "anomaly":
            # Match the legacy `lookback_days` offset (`ref - days`) so
            # the API-facing override stays source-compatible with
            # callers that already used `lookback_days`.
            start = ref - timedelta(days=days)
        else:
            # Weekly/monthly treat `days` as an N-day inclusive window.
            start = ref - timedelta(days=days - 1)
        end = ref
        return f"{start.isoformat()}/{end.isoformat()}", start, end

    if default_kind == "weekly":
        monday = ref - timedelta(days=ref.weekday())
        sunday = monday + timedelta(days=6)
        return monday.strftime("%G-W%V"), monday, sunday

    if default_kind == "monthly":
        first_of_this_month = ref.replace(day=1)
        last_of_prev = first_of_this_month - timedelta(days=1)
        first_of_prev = last_of_prev.replace(day=1)
        last_day = calendar.monthrange(first_of_prev.year, first_of_prev.month)[1]
        last_of_prev = first_of_prev.replace(day=last_day)
        return first_of_prev.strftime("%Y-%m"), first_of_prev, last_of_prev

    # anomaly default — 30-day lookback ending at ref.
    start = ref - timedelta(days=30)
    end = ref
    return f"{start.isoformat()}/{end.isoformat()}", start, end
