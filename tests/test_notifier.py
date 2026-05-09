"""Tests for src.core.notifier — Discord webhook + sensitive-data masking.

Covers:
- ``sanitize_error_message`` strips IBANs, bearer tokens, long digit
  runs.
- ``build_failure_payload`` produces a Discord-shaped dict and applies
  masking before the payload is built.
- ``send_discord_failure_notification`` is best-effort: timeouts,
  HTTP errors, and 4xx responses are logged but never raise.
- ``notify_failure_from_db`` reads the webhook URL from the singleton
  row and skips silently when none is configured.

No real Comdirect calls — every test mocks ``httpx.post``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.core.db.models import AppSettings
from src.core.notifier import (
    build_failure_payload,
    notify_failure_from_db,
    sanitize_error_message,
    send_discord_failure_notification,
)

WEBHOOK = "https://discord.com/api/webhooks/123/abc-token"


class TestSanitiseErrorMessage:
    def test_masks_real_de_iban(self):
        # Use a clearly synthetic-looking IBAN: 22-char DE pattern,
        # caught by the regex; gitleaks-allowlisted by tests/* path.
        msg = "Failure on account DE12345678901234567890 during sync"
        out = sanitize_error_message(msg)
        # Only the masked replacement DE•••••• should remain — no
        # 22-char DE+digit run anywhere in the output.
        import re

        assert re.search(r"\bDE\d{20}\b", out) is None
        assert "DE••••••" in out

    def test_masks_bearer_token(self):
        msg = "401 Unauthorized: Authorization: Bearer abc.DEF-123_456~xyz"
        out = sanitize_error_message(msg)
        assert "abc.DEF-123_456~xyz" not in out
        assert "Bearer ••••••" in out

    def test_masks_long_digit_run(self):
        msg = "Account 1234567890 returned no data"
        out = sanitize_error_message(msg)
        assert "1234567890" not in out
        assert "••••••" in out

    def test_short_digits_are_kept(self):
        # 8 digits is below the 9-min cap → free amounts ("1234.56")
        # stay legible.
        msg = "Failed at offset 12345678 with code 502"
        out = sanitize_error_message(msg)
        # 12345678 is 8 digits → kept.
        assert "12345678" in out
        assert "502" in out

    def test_empty_string_passthrough(self):
        assert sanitize_error_message("") == ""

    def test_idempotent(self):
        # Running it twice on the same string mustn't produce extra
        # mask markers or hide more characters.
        msg = "DE12345678901234567890 + Bearer abcdef1234"
        once = sanitize_error_message(msg)
        twice = sanitize_error_message(once)
        assert once == twice


class TestBuildFailurePayload:
    def test_shape_matches_discord_expectations(self):
        payload = build_failure_payload(
            run_kind="sync",
            run_id="run-abc",
            error_message="boom",
            occurred_at=datetime(2026, 5, 9, 12, 30, tzinfo=timezone.utc),
        )
        assert "content" in payload
        assert isinstance(payload["embeds"], list)
        assert len(payload["embeds"]) == 1
        embed = payload["embeds"][0]
        assert "title" in embed
        assert "description" in embed
        assert "color" in embed

    def test_masks_iban_in_error_message(self):
        payload = build_failure_payload(
            run_kind="sync",
            run_id="r1",
            error_message="Sync exploded for DE12345678901234567890",
            occurred_at=datetime.now(timezone.utc),
        )
        rendered = str(payload)
        # Critical assertion for the gitleaks-mandated masking: the raw
        # 22-char IBAN must not appear anywhere in the body sent to
        # Discord.
        import re

        assert re.search(r"\bDE\d{20}\b", rendered) is None
        assert "DE••••••" in rendered

    def test_truncates_oversize_description(self):
        huge = "x" * 5000
        payload = build_failure_payload(
            run_kind="sync",
            run_id="r1",
            error_message=huge,
            occurred_at=datetime.now(timezone.utc),
        )
        # 2000-char Discord limit honoured.
        assert len(payload["embeds"][0]["description"]) <= 2000


class TestSendDiscordFailureNotification:
    def test_posts_with_timeout_and_payload(self):
        with patch("src.core.notifier.httpx.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=204)
            send_discord_failure_notification(
                WEBHOOK,
                "sync",
                "run-1",
                "boom",
                datetime.now(timezone.utc),
            )
            assert mock_post.call_count == 1
            kwargs = mock_post.call_args.kwargs
            assert kwargs["timeout"] == 5.0
            # Payload sanity.
            assert "embeds" in kwargs["json"]

    def test_iban_never_leaves_in_post_body(self):
        # Critical: a fake IBAN flows through, the mock captures the
        # outgoing JSON, we assert the raw shape is gone.
        with patch("src.core.notifier.httpx.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=204)
            send_discord_failure_notification(
                WEBHOOK,
                "sync",
                "run-1",
                "Failure on account DE12345678901234567890",
                datetime.now(timezone.utc),
            )
            sent_json = mock_post.call_args.kwargs["json"]
            rendered = str(sent_json)
            import re

            assert re.search(r"\bDE\d{20}\b", rendered) is None

    def test_timeout_is_swallowed(self):
        with patch("src.core.notifier.httpx.post") as mock_post:
            mock_post.side_effect = httpx.TimeoutException("slow")
            # Must not raise.
            send_discord_failure_notification(
                WEBHOOK,
                "sync",
                "run-1",
                "boom",
                datetime.now(timezone.utc),
            )

    def test_http_error_is_swallowed(self):
        with patch("src.core.notifier.httpx.post") as mock_post:
            mock_post.side_effect = httpx.ConnectError("dns")
            send_discord_failure_notification(
                WEBHOOK,
                "sync",
                "run-1",
                "boom",
                datetime.now(timezone.utc),
            )

    def test_4xx_response_is_swallowed(self):
        with patch("src.core.notifier.httpx.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=401)
            # No raise; logs a warning.
            send_discord_failure_notification(
                WEBHOOK,
                "sync",
                "run-1",
                "boom",
                datetime.now(timezone.utc),
            )

    def test_empty_url_is_a_noop(self):
        with patch("src.core.notifier.httpx.post") as mock_post:
            send_discord_failure_notification(
                "",
                "sync",
                "run-1",
                "boom",
                datetime.now(timezone.utc),
            )
            assert mock_post.call_count == 0


# ---------------------------------------------------------------------------
# notify_failure_from_db — reads webhook_url from the AppSettings row
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("db_engine")
class TestNotifyFromDb:
    def test_no_settings_row_is_noop(self, db_engine):
        # No AppSettings row at all — must skip silently.
        with patch("src.core.notifier.httpx.post") as mock_post:
            notify_failure_from_db(
                db_engine,
                run_kind="sync",
                run_id="run-1",
                error_message="boom",
            )
            assert mock_post.call_count == 0

    def test_null_webhook_url_is_noop(self, db_engine, db_session):
        db_session.add(
            AppSettings(
                id=1,
                auto_apply_confidence=Decimal("0.60"),
                page_size=25,
                webhook_url=None,
            )
        )
        db_session.commit()

        with patch("src.core.notifier.httpx.post") as mock_post:
            notify_failure_from_db(
                db_engine,
                run_kind="sync",
                run_id="run-1",
                error_message="boom",
            )
            assert mock_post.call_count == 0

    def test_configured_url_triggers_post(self, db_engine, db_session):
        db_session.add(
            AppSettings(
                id=1,
                auto_apply_confidence=Decimal("0.60"),
                page_size=25,
                webhook_url=WEBHOOK,
            )
        )
        db_session.commit()

        with patch("src.core.notifier.httpx.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=204)
            notify_failure_from_db(
                db_engine,
                run_kind="sync",
                run_id="run-1",
                error_message="boom",
            )
            assert mock_post.call_count == 1
            assert mock_post.call_args.args[0] == WEBHOOK
