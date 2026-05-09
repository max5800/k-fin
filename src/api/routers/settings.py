"""App-settings endpoints — singleton config editable from the UI."""

from datetime import datetime, timezone
from decimal import Decimal

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.api.deps import Auth, CurrentPrincipal, CurrentUser, get_db
from src.core.db.models import AppSettings, User
from src.core.notifier import DISCORD_TIMEOUT_S, build_failure_payload

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


class SettingsOut(BaseModel):
    auto_apply_confidence: float
    page_size: int
    webhook_url: str | None = None

    @classmethod
    def from_row(cls, row: AppSettings) -> "SettingsOut":
        return cls(
            auto_apply_confidence=float(row.auto_apply_confidence),
            page_size=row.page_size,
            webhook_url=row.webhook_url,
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
def read_settings(db: Session = Depends(get_db)) -> SettingsOut:
    return SettingsOut.from_row(_get_or_create(db))


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
    return SettingsOut.from_row(row)


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
    _user: CurrentUser,
    db: Session = Depends(get_db),
) -> WebhookTestResult:
    """Send a demo failure-shaped message to the configured webhook.

    User-only — fires real outbound traffic to Discord, which we don't
    want a service token to be able to trigger automatically.

    Returns 200 with a structured success/error payload either way; the
    UI uses this to render a green/red toast next to the URL field.
    Configuration errors (no URL set, invalid URL) are surfaced as
    ``success=False`` with an explanatory ``error`` rather than HTTP 400
    so the frontend has a single response shape to handle.
    """
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
        response = httpx.post(
            row.webhook_url, json=payload, timeout=DISCORD_TIMEOUT_S
        )
    except httpx.TimeoutException:
        return WebhookTestResult(
            success=False,
            error=f"Discord did not respond within {DISCORD_TIMEOUT_S:.0f}s.",
        )
    except httpx.HTTPError as exc:
        return WebhookTestResult(success=False, error=f"HTTP error: {exc}")

    if response.status_code >= 400:
        return WebhookTestResult(
            success=False,
            status_code=response.status_code,
            error=f"Discord returned HTTP {response.status_code}",
        )
    return WebhookTestResult(success=True, status_code=response.status_code)
