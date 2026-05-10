"""Tests for the app-settings router (/api/v1/settings).

Settings is a single-row table that stores user-tunable knobs the UI
can edit at runtime. The PUT body lets a caller update one knob at a
time — fields default to None and are only applied when explicitly set.
"""

from __future__ import annotations

import os
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from src.core.db.models import AppSettings

AUTH = {"Authorization": "Bearer test-secret"}
JWT_SECRET = "test-jwt-secret-min-32chars-long-aaaaaaaa"


@pytest.fixture
def api_client(db_engine):
    """Spin up the api FastAPI app with API_TOKEN + JWT_SECRET wired up.

    The JWT secret is included so tests can mint real user bearer
    tokens via the ``user_auth`` fixture below — required by the
    webhook_url field, which is user-only.
    """
    import sys

    from src.core.db import get_db

    def _override_get_db():
        with Session(db_engine) as session:
            yield session

    db_url = db_engine.url.render_as_string(hide_password=False)
    env = {
        "API_TOKEN": "test-secret",
        "DATABASE_URL": db_url,
        "JWT_SECRET": JWT_SECRET,
    }
    with patch.dict(os.environ, env):
        from src.api.app import create_app
        from src.core.config import Settings as _SettingsCls

        app = create_app()

        # Sync every loaded `settings` instance — defends against test
        # pollution from suites that reload `src.core.config`.
        seen: set[int] = set()
        for mod in list(sys.modules.values()):
            if mod is None:
                continue
            cur = getattr(mod, "settings", None)
            if isinstance(cur, _SettingsCls) and id(cur) not in seen:
                seen.add(id(cur))
                cur.api_token = "test-secret"
                cur.database_url = db_url
                cur.jwt_secret = JWT_SECRET

        app.dependency_overrides[get_db] = _override_get_db
        client = TestClient(app)
        yield client
        app.dependency_overrides.clear()


@pytest.fixture
def user_auth(db_engine, api_client):
    """Mint a real JWT bearer header for a freshly-seeded user."""
    import sys
    import uuid as _uuid

    from src.core.db.models import User

    user_id = _uuid.uuid4().hex
    with Session(db_engine) as s:
        s.add(
            User(
                id=user_id,
                email="settings-tester@example.com",
                password_hash="dummy-not-used",
                display_name="Settings Tester",
                is_active=True,
            )
        )
        s.commit()

    from src.core.config import Settings as _SettingsCls

    seen: set[int] = set()
    for mod in list(sys.modules.values()):
        if mod is None:
            continue
        cur = getattr(mod, "settings", None)
        if isinstance(cur, _SettingsCls) and id(cur) not in seen:
            seen.add(id(cur))
            cur.jwt_secret = JWT_SECRET

    from src.api.auth.jwt import issue_token

    yield {"Authorization": f"Bearer {issue_token(user_id)}"}


class TestReadSettings:
    def test_requires_auth(self, api_client):
        resp = api_client.get("/api/v1/settings")
        assert resp.status_code == 401

    def test_returns_defaults_when_row_missing(self, api_client, db_engine):
        resp = api_client.get("/api/v1/settings", headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["page_size"] == 25
        assert body["auto_apply_confidence"] == pytest.approx(0.60)

        # The GET should have lazily created the singleton row.
        with Session(db_engine) as s:
            row = s.get(AppSettings, 1)
            assert row is not None
            assert row.page_size == 25

    def test_returns_persisted_row(self, api_client, db_engine):
        with Session(db_engine) as s:
            s.add(
                AppSettings(
                    id=1,
                    auto_apply_confidence=Decimal("0.80"),
                    page_size=100,
                )
            )
            s.commit()

        resp = api_client.get("/api/v1/settings", headers=AUTH)
        body = resp.json()
        assert body["page_size"] == 100
        assert body["auto_apply_confidence"] == pytest.approx(0.80)


class TestUpdatePageSize:
    def test_requires_auth(self, api_client):
        resp = api_client.put("/api/v1/settings", json={"page_size": 50})
        assert resp.status_code == 401

    def test_updates_page_size(self, api_client, db_engine):
        resp = api_client.put(
            "/api/v1/settings",
            json={"page_size": 50},
            headers=AUTH,
        )
        assert resp.status_code == 200
        assert resp.json()["page_size"] == 50

        with Session(db_engine) as s:
            row = s.get(AppSettings, 1)
            assert row.page_size == 50

    def test_update_only_page_size_keeps_confidence(self, api_client, db_engine):
        # Pre-populate with a non-default confidence value.
        with Session(db_engine) as s:
            s.add(
                AppSettings(
                    id=1,
                    auto_apply_confidence=Decimal("0.80"),
                    page_size=25,
                )
            )
            s.commit()

        resp = api_client.put(
            "/api/v1/settings",
            json={"page_size": 200},
            headers=AUTH,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["page_size"] == 200
        # Untouched.
        assert body["auto_apply_confidence"] == pytest.approx(0.80)

    def test_below_minimum_returns_422(self, api_client):
        resp = api_client.put(
            "/api/v1/settings",
            json={"page_size": 9},
            headers=AUTH,
        )
        assert resp.status_code == 422

    def test_at_minimum_succeeds(self, api_client):
        resp = api_client.put(
            "/api/v1/settings",
            json={"page_size": 10},
            headers=AUTH,
        )
        assert resp.status_code == 200
        assert resp.json()["page_size"] == 10

    def test_at_maximum_succeeds(self, api_client):
        resp = api_client.put(
            "/api/v1/settings",
            json={"page_size": 200},
            headers=AUTH,
        )
        assert resp.status_code == 200
        assert resp.json()["page_size"] == 200

    def test_above_maximum_returns_422(self, api_client):
        resp = api_client.put(
            "/api/v1/settings",
            json={"page_size": 201},
            headers=AUTH,
        )
        assert resp.status_code == 422

    def test_negative_returns_422(self, api_client):
        resp = api_client.put(
            "/api/v1/settings",
            json={"page_size": -1},
            headers=AUTH,
        )
        assert resp.status_code == 422


class TestUpdateBoth:
    def test_can_update_both_fields_in_one_call(self, api_client, db_engine):
        resp = api_client.put(
            "/api/v1/settings",
            json={"page_size": 100, "auto_apply_confidence": 0.75},
            headers=AUTH,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["page_size"] == 100
        assert body["auto_apply_confidence"] == pytest.approx(0.75)

    def test_empty_body_is_a_noop_returning_current(self, api_client, db_engine):
        with Session(db_engine) as s:
            s.add(
                AppSettings(
                    id=1,
                    auto_apply_confidence=Decimal("0.42"),
                    page_size=42,
                )
            )
            s.commit()

        resp = api_client.put("/api/v1/settings", json={}, headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["page_size"] == 42
        assert body["auto_apply_confidence"] == pytest.approx(0.42)


# ---------------------------------------------------------------------------
# webhook_url — Stream D
# ---------------------------------------------------------------------------


VALID_DISCORD_URL = "https://discord.com/api/webhooks/123/abc-token"
VALID_DISCORDAPP_URL = "https://discordapp.com/api/webhooks/123/abc-token"


class TestWebhookUrlSetting:
    def test_default_is_null(self, api_client):
        resp = api_client.get("/api/v1/settings", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["webhook_url"] is None

    def test_set_valid_discord_url(self, api_client, db_engine, user_auth):
        resp = api_client.put(
            "/api/v1/settings",
            json={"webhook_url": VALID_DISCORD_URL},
            headers=user_auth,
        )
        assert resp.status_code == 200
        assert resp.json()["webhook_url"] == VALID_DISCORD_URL

        with Session(db_engine) as s:
            row = s.get(AppSettings, 1)
            assert row.webhook_url == VALID_DISCORD_URL

    def test_set_valid_discordapp_url(self, api_client, user_auth):
        resp = api_client.put(
            "/api/v1/settings",
            json={"webhook_url": VALID_DISCORDAPP_URL},
            headers=user_auth,
        )
        assert resp.status_code == 200
        assert resp.json()["webhook_url"] == VALID_DISCORDAPP_URL

    def test_reject_unrelated_https_url(self, api_client, user_auth):
        resp = api_client.put(
            "/api/v1/settings",
            json={"webhook_url": "https://evil.example.com/api/webhooks/1/x"},
            headers=user_auth,
        )
        assert resp.status_code == 400
        assert "discord.com" in resp.json()["detail"].lower()

    def test_reject_http_scheme(self, api_client, user_auth):
        resp = api_client.put(
            "/api/v1/settings",
            json={"webhook_url": "http://discord.com/api/webhooks/1/x"},
            headers=user_auth,
        )
        assert resp.status_code == 400

    def test_reject_subdomain_typosquatting(self, api_client, user_auth):
        # discord.com.evil.com would startswith() pass if we used the
        # bare host; the prefix check enforces the full path too.
        resp = api_client.put(
            "/api/v1/settings",
            json={
                "webhook_url": "https://discord.com.evil.example/api/webhooks/1/x"
            },
            headers=user_auth,
        )
        assert resp.status_code == 400

    def test_clear_via_empty_string(self, api_client, db_engine, user_auth):
        api_client.put(
            "/api/v1/settings",
            json={"webhook_url": VALID_DISCORD_URL},
            headers=user_auth,
        )
        resp = api_client.put(
            "/api/v1/settings",
            json={"webhook_url": ""},
            headers=user_auth,
        )
        assert resp.status_code == 200
        assert resp.json()["webhook_url"] is None
        with Session(db_engine) as s:
            row = s.get(AppSettings, 1)
            assert row.webhook_url is None

    def test_omitted_field_keeps_existing_value(self, api_client, db_engine):
        # page_size update by service token must NOT trigger the
        # webhook user-only check (webhook_url is omitted entirely).
        # The response webhook_url is masked because the caller is a
        # service principal (see TestWebhookUrlMasking below).
        with Session(db_engine) as s:
            s.add(
                AppSettings(
                    id=1,
                    auto_apply_confidence=Decimal("0.60"),
                    page_size=25,
                    webhook_url=VALID_DISCORD_URL,
                )
            )
            s.commit()

        resp = api_client.put(
            "/api/v1/settings",
            json={"page_size": 50},
            headers=AUTH,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["page_size"] == 50
        # Service token gets the masked form; the secret tail stays hidden.
        assert body["webhook_url"] is not None
        assert body["webhook_url"] != VALID_DISCORD_URL
        assert "…" in body["webhook_url"]
        assert "abc-token" not in body["webhook_url"]
        # The DB row is unchanged — masking is presentation-only.
        with Session(db_engine) as s:
            row = s.get(AppSettings, 1)
            assert row.webhook_url == VALID_DISCORD_URL

    def test_oversize_url_rejected(self, api_client, user_auth):
        too_long = (
            "https://discord.com/api/webhooks/" + "a" * 600
        )
        resp = api_client.put(
            "/api/v1/settings",
            json={"webhook_url": too_long},
            headers=user_auth,
        )
        assert resp.status_code == 422

    def test_service_token_cannot_set_webhook(self, api_client):
        # Service tokens (MCP / scheduler) get explicit 403 when they
        # try to mutate the webhook URL.
        resp = api_client.put(
            "/api/v1/settings",
            json={"webhook_url": VALID_DISCORD_URL},
            headers=AUTH,
        )
        assert resp.status_code == 403


class TestWebhookTestEndpoint:
    def test_requires_auth(self, api_client):
        resp = api_client.post("/api/v1/settings/webhook/test")
        assert resp.status_code == 401

    def test_service_token_rejected(self, api_client):
        resp = api_client.post(
            "/api/v1/settings/webhook/test", headers=AUTH
        )
        assert resp.status_code == 403

    def test_returns_failure_when_no_url_configured(self, api_client, user_auth):
        resp = api_client.post(
            "/api/v1/settings/webhook/test", headers=user_auth
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert "no webhook url" in body["error"].lower()

    def test_calls_httpx_when_url_set(
        self, api_client, db_engine, monkeypatch, user_auth
    ):
        with Session(db_engine) as s:
            s.add(
                AppSettings(
                    id=1,
                    auto_apply_confidence=Decimal("0.60"),
                    page_size=25,
                    webhook_url=VALID_DISCORD_URL,
                )
            )
            s.commit()

        captured = {}

        class _StubResponse:
            status_code = 204

        def fake_post(url, json, timeout):
            captured["url"] = url
            captured["json"] = json
            captured["timeout"] = timeout
            return _StubResponse()

        from src.api.routers import settings as settings_module

        monkeypatch.setattr(settings_module.httpx, "post", fake_post)

        resp = api_client.post(
            "/api/v1/settings/webhook/test", headers=user_auth
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["status_code"] == 204
        assert captured["url"] == VALID_DISCORD_URL
        assert "embeds" in captured["json"]

    def test_reports_discord_4xx_as_failure(
        self, api_client, db_engine, monkeypatch, user_auth
    ):
        with Session(db_engine) as s:
            s.add(
                AppSettings(
                    id=1,
                    auto_apply_confidence=Decimal("0.60"),
                    page_size=25,
                    webhook_url=VALID_DISCORD_URL,
                )
            )
            s.commit()

        class _StubResponse:
            status_code = 401

        def fake_post(url, json, timeout):
            return _StubResponse()

        from src.api.routers import settings as settings_module

        monkeypatch.setattr(settings_module.httpx, "post", fake_post)

        resp = api_client.post(
            "/api/v1/settings/webhook/test", headers=user_auth
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert body["status_code"] == 401


# ---------------------------------------------------------------------------
# webhook_url masking — service-token callers get the secret tail hidden
# ---------------------------------------------------------------------------


class TestWebhookUrlMasking:
    """Service-token callers must not see the raw Discord bot-token."""

    def _seed_url(self, db_engine, url: str) -> None:
        with Session(db_engine) as s:
            row = s.get(AppSettings, 1)
            if row is None:
                s.add(
                    AppSettings(
                        id=1,
                        auto_apply_confidence=Decimal("0.60"),
                        page_size=25,
                        webhook_url=url,
                    )
                )
            else:
                row.webhook_url = url
            s.commit()

    def test_get_returns_full_url_for_jwt_user(
        self, api_client, db_engine, user_auth
    ):
        self._seed_url(db_engine, VALID_DISCORD_URL)
        resp = api_client.get("/api/v1/settings", headers=user_auth)
        assert resp.status_code == 200
        # JWT users edit the URL in the UI, so they need the raw value.
        assert resp.json()["webhook_url"] == VALID_DISCORD_URL

    def test_get_masks_url_for_service_token(self, api_client, db_engine):
        self._seed_url(db_engine, VALID_DISCORD_URL)
        resp = api_client.get("/api/v1/settings", headers=AUTH)
        assert resp.status_code == 200
        masked = resp.json()["webhook_url"]
        assert masked is not None
        assert masked != VALID_DISCORD_URL
        # Domain visible — operator can confirm the host is right.
        assert masked.startswith("https://discord.com/api/webhooks/123/")
        # Ellipsis marker plus only the last 6 chars of the token.
        assert "…" in masked
        # Real secret never appears in the masked form.
        assert "abc-token" not in masked
        # Last 6 chars of "abc-token" are "c-token" (7) → "-token" (6).
        assert masked.endswith("-token")

    def test_get_masks_short_token_completely(self, api_client, db_engine):
        # If the token segment is <=6 chars, mask the entire tail rather
        # than echoing the bulk of it back.
        short_url = "https://discord.com/api/webhooks/9/abc"
        self._seed_url(db_engine, short_url)
        resp = api_client.get("/api/v1/settings", headers=AUTH)
        assert resp.status_code == 200
        masked = resp.json()["webhook_url"]
        assert masked == "https://discord.com/api/webhooks/9/…"

    def test_get_passes_through_null_for_service_token(
        self, api_client, db_engine
    ):
        self._seed_url(db_engine, VALID_DISCORD_URL)
        # Clear via JWT user, then read back via service token — must be null,
        # not an empty mask.
        api_client.put(
            "/api/v1/settings",
            json={"webhook_url": ""},
            headers={
                "Authorization": "Bearer test-secret",  # ignored — set via user_auth
            }
            | {},
        )
        # Use a direct DB clear since the line above uses service-token auth
        # which is forbidden for setting webhook_url.
        with Session(db_engine) as s:
            row = s.get(AppSettings, 1)
            row.webhook_url = None
            s.commit()
        resp = api_client.get("/api/v1/settings", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["webhook_url"] is None

    def test_put_response_masks_for_service_token(self, api_client, db_engine):
        # Service-token PUT can't mutate webhook_url, but a page_size PUT
        # echoes the row back — that response must also be masked.
        self._seed_url(db_engine, VALID_DISCORD_URL)
        resp = api_client.put(
            "/api/v1/settings",
            json={"page_size": 75},
            headers=AUTH,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["page_size"] == 75
        assert body["webhook_url"] != VALID_DISCORD_URL
        assert "…" in body["webhook_url"]
        assert "abc-token" not in body["webhook_url"]

    def test_put_response_keeps_full_for_jwt_user(
        self, api_client, db_engine, user_auth
    ):
        self._seed_url(db_engine, VALID_DISCORD_URL)
        resp = api_client.put(
            "/api/v1/settings",
            json={"page_size": 80},
            headers=user_auth,
        )
        assert resp.status_code == 200
        assert resp.json()["webhook_url"] == VALID_DISCORD_URL
