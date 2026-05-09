"""Discord webhook notifier — best-effort failure alerts.

Called by the scheduler/orchestrator when a sync or agent run flips to
FAILED. The contract is intentionally narrow:

- *Synchronous* httpx call with a 5-second hard timeout. We block the
  failure path for at most 5s; we never want a webhook outage to wedge
  a sync.
- *Best-effort*: every error path swallows the exception, logs a
  warning, and returns. The caller's terminal status write (FAILED on
  the run row) is the source of truth — the webhook is a courtesy.
- *Sensitive-data masking*: error messages can carry IBANs, account
  numbers, or tokens lifted out of stack traces. We strip those before
  posting. The free-text body still goes out so the user has enough
  context to act, just without leaking PII to a third-party chat
  service.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Final

import httpx
from sqlalchemy import Engine
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Discord-side timeout. The webhook endpoint is fast (<200 ms) on a good
# day; 5s is generous and bounds total damage if Discord stalls.
DISCORD_TIMEOUT_S: Final[float] = 5.0

# Discord red (decimal) — surfaces in the embed sidebar so the message
# reads as an alert, not an info ping.
DISCORD_COLOR_RED: Final[int] = 0xE74C3C

# Embed length limits per Discord docs. We cap aggressively to keep
# truncated stack traces from getting clipped mid-IBAN (which would
# defeat the masking).
_DESCRIPTION_MAX = 2000
_TITLE_MAX = 256

# ---------------------------------------------------------------------------
# Sensitive-data scrubbing
# ---------------------------------------------------------------------------
#
# Patterns we mask before any text leaves the process:
#
# - DE IBANs: 22-char (DE + 20 digits). Anchored with \b so partial
#   matches inside longer sequences don't get rewritten.
# - Generic IBANs: 2 letters + 2 digits + up to 30 alphanumerics. Looser
#   than the DE rule and catches AT/CH/etc. without false-positiving on
#   ordinary words because the leading-digits requirement is strict.
# - Bearer tokens: "Bearer <stuff>" — Authorization headers occasionally
#   land in stack traces.
# - Long digit runs (≥9): account-number stand-ins. Conservative; we
#   don't try to identify amounts vs. account IDs since the failure
#   message rarely contains useful numbers anyway.

_DE_IBAN = re.compile(r"\bDE\d{20}\b")
_GENERIC_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")
_BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-_\.~+/=]{8,}")
_LONG_DIGITS = re.compile(r"\b\d{9,}\b")


def sanitize_error_message(message: str) -> str:
    """Strip IBAN / account / token shapes out of free-form text.

    Intentionally lossy: we'd rather drop signal than leak PII to
    Discord. Passes already-clean strings through unchanged.
    """
    if not message:
        return ""
    out = _DE_IBAN.sub("DE••••••", message)
    out = _GENERIC_IBAN.sub("••IBAN••", out)
    out = _BEARER_TOKEN.sub("Bearer ••••••", out)
    out = _LONG_DIGITS.sub("••••••", out)
    return out


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def build_failure_payload(
    *,
    run_kind: str,
    run_id: str,
    error_message: str,
    occurred_at: datetime,
) -> dict:
    """Shape the JSON body Discord expects for a webhook embed.

    Kept module-public so the test-send endpoint can reuse the exact
    same shape without re-duplicating it inline.
    """
    safe_error = sanitize_error_message(error_message or "(no error message)")
    title = _truncate(f"k-fin: {run_kind} failed", _TITLE_MAX)
    description = _truncate(
        f"**Run-ID:** `{run_id}`\n"
        f"**When:** {occurred_at.isoformat()}\n\n"
        f"**Error:**\n```\n{safe_error}\n```",
        _DESCRIPTION_MAX,
    )
    return {
        "content": f"k-fin sync alert — {run_kind} failed",
        "embeds": [
            {
                "title": title,
                "description": description,
                "color": DISCORD_COLOR_RED,
            }
        ],
    }


def send_discord_failure_notification(
    webhook_url: str,
    run_kind: str,
    run_id: str,
    error_message: str,
    occurred_at: datetime,
) -> None:
    """POST a failure alert to a Discord webhook. Best-effort; never raises.

    Args:
        webhook_url: full Discord webhook URL (validated upstream by the
            settings router).
        run_kind: free-form label — "sync", "backfill", "agent:weekly", …
        run_id: opaque ID the user can grep for in logs / the runs table.
        error_message: the message to embed; gets masked before sending.
        occurred_at: timestamp the failure was detected.

    The function returns ``None`` and *swallows every exception*. The
    caller's terminal status write must already have happened — the
    webhook is a courtesy, never a gate.
    """
    if not webhook_url:
        # Defensive: callers usually filter, but a None/empty URL here
        # is a no-op rather than an error.
        return

    payload = build_failure_payload(
        run_kind=run_kind,
        run_id=run_id,
        error_message=error_message,
        occurred_at=occurred_at,
    )

    try:
        response = httpx.post(
            webhook_url,
            json=payload,
            timeout=DISCORD_TIMEOUT_S,
        )
        # Discord returns 204 No Content on success, 200 with a message
        # body when ?wait=true is set. Anything else is a soft failure.
        if response.status_code >= 400:
            logger.warning(
                "Discord webhook returned HTTP %d for %s/%s",
                response.status_code,
                run_kind,
                run_id,
            )
    except httpx.TimeoutException:
        logger.warning(
            "Discord webhook timed out after %.1fs for %s/%s",
            DISCORD_TIMEOUT_S,
            run_kind,
            run_id,
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "Discord webhook failed for %s/%s: %s",
            run_kind,
            run_id,
            exc,
        )
    except Exception as exc:  # pragma: no cover — last-resort safety net
        logger.warning(
            "Unexpected error sending Discord webhook for %s/%s: %s",
            run_kind,
            run_id,
            exc,
        )


def _load_webhook_url(engine: Engine) -> str | None:
    """Read the configured Discord webhook URL from app_settings.

    Returns ``None`` when the column is empty/null, the singleton row is
    missing, or the lookup itself fails — every variant means "stay
    silent" rather than "explode the failure path".
    """
    try:
        from src.core.db.models import AppSettings

        with Session(engine) as session:
            row = session.get(AppSettings, 1)
            if row is None:
                return None
            url = row.webhook_url
            return url if url else None
    except Exception:
        logger.exception("Failed to read webhook_url from app_settings")
        return None


def notify_failure_from_db(
    engine: Engine,
    *,
    run_kind: str,
    run_id: str,
    error_message: str,
    occurred_at: datetime | None = None,
) -> None:
    """Convenience: load webhook URL from DB, fire notification if set.

    Used by the sync/backfill/reaper failure paths so each call site
    stays a single line. Returns silently when no webhook is configured.
    Never raises.
    """
    url = _load_webhook_url(engine)
    if not url:
        return
    send_discord_failure_notification(
        url,
        run_kind,
        run_id,
        error_message,
        occurred_at or datetime.now(timezone.utc),
    )
