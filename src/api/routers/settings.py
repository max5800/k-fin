"""App-settings endpoints — singleton config editable from the UI."""

import threading
import time
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.api.deps import Auth, CurrentPrincipal, CurrentUser, ServicePrincipal, get_db
from src.core.db.models import AppSettings, User
from src.core.logging import get_logger
from src.core.notifier import DISCORD_TIMEOUT_S, build_failure_payload

logger = get_logger("settings")

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Auth])

# Hard cap mirrors the transactions-list `limit` ceiling and prevents the
# UI from saving a setting it can't actually use against the API.
PAGE_SIZE_MIN = 10
PAGE_SIZE_MAX = 200
PAGE_SIZE_DEFAULT = 25

# Discord-only for the first iteration. Both hostnames are official —
# `discordapp.com` is the legacy alias the dashboard still hands out for
# older webhooks. Anything else gets rejected up front so we don't ship
# user input straight to a random URL on failure.
ALLOWED_WEBHOOK_PREFIXES = (
    "https://discord.com/api/webhooks/",
    "https://discordapp.com/api/webhooks/",
)
WEBHOOK_URL_MAX_LEN = 500

# ---------------------------------------------------------------------------
# Per-user rate limit for POST /settings/webhook/test
# ---------------------------------------------------------------------------
#
# Why in-memory and not a DB column:
#   - Single-worker deployment (the parent app already keeps in-memory
#     state in `_pending_sessions`); horizontally scaling the API would
#     break TAN-in-the-loop sessions long before this counter matters.
#   - The cap is anti-spam, not a security boundary. A worker restart
#     resets the bucket, but 5/min is still 100x stricter than no limit.
#   - Adding a DB column + Alembic migration for a hot-path counter is
#     overkill and introduces a write per call to a singleton table.
#
# Implementation: a sliding window of the last N timestamps per user.
# Tests can clear it via `_clear_rate_limit_state()`.

WEBHOOK_TEST_RATE_LIMIT_MAX = 5
WEBHOOK_TEST_RATE_LIMIT_WINDOW_S = 60.0

_rate_limit_lock = threading.Lock()
_rate_limit_buckets: dict[str, deque[float]] = {}


def _clear_rate_limit_state() -> None:
    """Test helper: wipe every per-user bucket so cases stay isolated."""
    with _rate_limit_lock:
        _rate_limit_buckets.clear()


def _check_webhook_test_rate_limit(user_id: str, *, now: float | None = None) -> None:
    """Raise 429 with Retry-After if the user has exceeded the test cap.

    Sliding-window: keep a deque of the last <max> hit timestamps; drop
    anything outside the window before deciding. Atomic under a single
    lock — at this call rate the contention is negligible.
    """
    current = time.monotonic() if now is None else now
    cutoff = current - WEBHOOK_TEST_RATE_LIMIT_WINDOW_S
    with _rate_limit_lock:
        bucket = _rate_limit_buckets.setdefault(user_id, deque())
        # Drop expired entries from the front.
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= WEBHOOK_TEST_RATE_LIMIT_MAX:
            # Retry-After = seconds until the *oldest* hit ages out.
            retry_after = max(1, int(bucket[0] + WEBHOOK_TEST_RATE_LIMIT_WINDOW_S - current) + 1)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Webhook test rate limit exceeded "
                    f"({WEBHOOK_TEST_RATE_LIMIT_MAX}/min). Retry in {retry_after}s."
                ),
                headers={"Retry-After": str(retry_after)},
            )
        bucket.append(current)


def _validate_webhook_url(url: str) -> str:
    """Reject anything that isn't an official Discord webhook URL."""
    if len(url) > WEBHOOK_URL_MAX_LEN:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"webhook_url too long (max {WEBHOOK_URL_MAX_LEN} chars)",
        )
    if not any(url.startswith(p) for p in ALLOWED_WEBHOOK_PREFIXES):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "webhook_url must start with "
                "https://discord.com/api/webhooks/ or "
                "https://discordapp.com/api/webhooks/"
            ),
        )
    return url


def _mask_webhook_url(url: str | None) -> str | None:
    """Mask a Discord webhook URL so the bot-token suffix isn't exposed.

    Service-token callers (MCP, scheduler) don't need the full URL — they
    never post to it directly. Returning the secret-bearing tail to them
    would defeat the whole point of keeping it user-only on PUT.

    The masked form keeps the full URL prefix up to the last segment,
    plus the last 6 chars of the token, joined with an ellipsis. That
    leaves enough surface for an operator to confirm the URL is "set
    and looks plausible" without leaking the actual secret.
    """
    if not url:
        return url
    # Trim trailing slash so the split below behaves predictably.
    trimmed = url.rstrip("/")
    # Anything not webhook-shaped: be paranoid, mask everything after the host.
    last_slash = trimmed.rfind("/")
    if last_slash <= 0:
        return url  # nothing reasonable to split on; leave alone
    head = trimmed[: last_slash + 1]
    tail = trimmed[last_slash + 1 :]
    if len(tail) <= 6:
        # Token shorter than the suffix we'd reveal — mask the whole tail.
        return f"{head}…"
    return f"{head}…{tail[-6:]}"


class SettingsOut(BaseModel):
    auto_apply_confidence: float
    page_size: int
    webhook_url: str | None = None

    @classmethod
    def from_row(
        cls,
        row: AppSettings,
        *,
        principal: User | ServicePrincipal | None = None,
    ) -> "SettingsOut":
        webhook = row.webhook_url
        # Never hand the raw Discord webhook URL (bot-token-bearing) to a
        # non-human caller. JWT users (and the legacy "no principal known"
        # path used by old tests) keep getting the full value.
        if isinstance(principal, ServicePrincipal):
            webhook = _mask_webhook_url(webhook)
        return cls(
            auto_apply_confidence=float(row.auto_apply_confidence),
            page_size=row.page_size,
            webhook_url=webhook,
        )


class SettingsUpdate(BaseModel):
    # Both fields optional so the UI can update one knob at a time without
    # resending the other (None means "leave as is").
    auto_apply_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    page_size: int | None = Field(
        default=None, ge=PAGE_SIZE_MIN, le=PAGE_SIZE_MAX
    )
    # webhook_url uses a sentinel-vs-null distinction the model itself
    # can't express: explicit `null` means "clear it", omission means
    # "leave it". Routed through Field with a default of "__unset__"
    # would cross the type system; instead we read it from the raw body
    # in the PUT handler.
    webhook_url: str | None = Field(default=None, max_length=WEBHOOK_URL_MAX_LEN)


def _get_or_create(db: Session) -> AppSettings:
    row = db.get(AppSettings, 1)
    if row is None:
        row = AppSettings(
            id=1,
            auto_apply_confidence=Decimal("0.60"),
            page_size=PAGE_SIZE_DEFAULT,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


@router.get("", response_model=SettingsOut)
def read_settings(
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
) -> SettingsOut:
    """Return the singleton settings row.

    Service-token callers receive a masked ``webhook_url``: the host
    plus the last 6 chars of the token segment. JWT users get the full
    URL since they need to copy it back into the UI form on edit.
    """
    return SettingsOut.from_row(_get_or_create(db), principal=principal)


@router.put("", response_model=SettingsOut)
def update_settings(
    payload: SettingsUpdate,
    principal: CurrentPrincipal,
    db: Session = Depends(get_db),
) -> SettingsOut:
    """Update editable settings.

    Most knobs accept either a JWT user *or* the static API token (so
    MCP / scheduler stay unaffected). The ``webhook_url`` field is
    user-only — a service principal that tries to set or clear it
    receives 403, since the URL is personal data and should never be
    flipped by an automated caller.
    """
    row = _get_or_create(db)
    if payload.auto_apply_confidence is not None:
        try:
            row.auto_apply_confidence = Decimal(str(payload.auto_apply_confidence))
        except (ValueError, TypeError) as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid auto_apply_confidence: {e}",
            )
    if payload.page_size is not None:
        row.page_size = payload.page_size
    # webhook_url: support both "set to URL" and "clear via empty string".
    # An omitted field stays None at the model level, which is
    # indistinguishable from "clear" — so we treat None as "no change"
    # and accept "" (empty string) as the explicit clear signal.
    if payload.webhook_url is not None:
        if not isinstance(principal, User):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="webhook_url can only be set by a logged-in user.",
            )
        if payload.webhook_url == "":
            row.webhook_url = None
        else:
            row.webhook_url = _validate_webhook_url(payload.webhook_url)
    db.commit()
    db.refresh(row)
    return SettingsOut.from_row(row, principal=principal)


# ---------------------------------------------------------------------------
# Webhook test-send — manual sanity check for the configured URL
# ---------------------------------------------------------------------------


class WebhookTestResult(BaseModel):
    """Outcome of POST /settings/webhook/test.

    `success` is True only on a 2xx response from Discord. `status_code`
    is None when the request never made it (timeout, DNS, connection
    refused), otherwise the HTTP status as returned. `error` carries a
    short human-readable reason on failure.
    """

    success: bool
    status_code: int | None = None
    error: str | None = None


@router.post("/webhook/test", response_model=WebhookTestResult)
def test_webhook(
    user: CurrentUser,
    db: Session = Depends(get_db),
) -> WebhookTestResult:
    """Send a demo failure-shaped message to the configured webhook.

    User-only — fires real outbound traffic to Discord, which we don't
    want a service token to be able to trigger automatically. Per-user
    rate limit (5/min sliding window) keeps a fat-finger UI loop or a
    runaway script from getting the webhook IP-banned by Discord.

    Returns 200 with a structured success/error payload either way; the
    UI uses this to render a green/red toast next to the URL field.
    Configuration errors (no URL set, invalid URL) are surfaced as
    ``success=False`` with an explanatory ``error`` rather than HTTP 400
    so the frontend has a single response shape to handle.
    """
    # Per-user 5/min cap. Raises 429 with Retry-After on overflow.
    _check_webhook_test_rate_limit(user.id)
    # Audit log every attempt — useful when correlating Discord-side bans
    # with k-fin activity. ``user.id`` is the canonical UUID, no PII.
    logger.info("webhook-test invoked by user_id=%s", user.id)

    row = _get_or_create(db)
    if not row.webhook_url:
        return WebhookTestResult(
            success=False,
            error="No webhook URL configured.",
        )
    # Re-validate at send-time too — the URL could have been written
    # directly to the DB, bypassing the PUT validator.
    if not any(
        row.webhook_url.startswith(p) for p in ALLOWED_WEBHOOK_PREFIXES
    ):
        return WebhookTestResult(
            success=False,
            error="Stored webhook_url does not match an allowed Discord prefix.",
        )

    payload = build_failure_payload(
        run_kind="test",
        run_id="webhook-self-test",
        error_message=(
            "This is a k-fin self-test message — your webhook is wired up. "
            "Real failure pings include the failed run-ID, the agent name, "
            "and a sanitised error excerpt."
        ),
        occurred_at=datetime.now(timezone.utc),
    )
    try:
        response_obj = httpx.post(
            row.webhook_url, json=payload, timeout=DISCORD_TIMEOUT_S
        )
    except httpx.TimeoutException:
        return WebhookTestResult(
            success=False,
            error=f"Discord did not respond within {DISCORD_TIMEOUT_S:.0f}s.",
        )
    except httpx.HTTPError as exc:
        return WebhookTestResult(success=False, error=f"HTTP error: {exc}")

    if response_obj.status_code >= 400:
        return WebhookTestResult(
            success=False,
            status_code=response_obj.status_code,
            error=f"Discord returned HTTP {response_obj.status_code}",
        )
    return WebhookTestResult(success=True, status_code=response_obj.status_code)
