"""Tests for the PayPal CSV import endpoint (``POST /import/paypal-csv``).

Covers the auth guard, the happy path (funding rows skipped, real
transactions ingested + normalized in one call), idempotent re-upload,
the 422 on a malformed CSV, and the 400 on an empty file. Backed by a
throw-away Postgres (testcontainers).
"""

from __future__ import annotations

import csv
import io
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

AUTH = {"Authorization": "Bearer test-secret"}

# A minimal PayPal "Kontoauszug" column subset — the parser maps by
# header name, so only the columns under test need to be present.
_COLUMNS = [
    "Datum", "Beschreibung", "Währung", "Brutto", "Entgelt", "Netto",
    "Transaktionscode", "Name",
]
# Two real merchant payments and one funding row (empty Name → skipped).
_PAYMENT_A = ["08.05.2026", "Zahlung im Einzugsverfahren mit Zahlungsrechnung",
              "EUR", "-19,99", "0,00", "-19,99", "TXN-A", "Test Merchant Ltd"]
_PAYMENT_B = ["09.05.2026", "PayPal Express-Zahlung",
              "EUR", "-5,00", "0,00", "-5,00", "TXN-B", "Another Shop"]
_FUNDING = ["08.05.2026", "Bankgutschrift auf PayPal-Konto",
            "EUR", "19,99", "0,00", "19,99", "TXN-FUND", ""]


def _make_csv(rows: list[list[str]], columns: list[str] = _COLUMNS) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _upload(content: bytes) -> dict:
    return {"file": ("kontoauszug.csv", content, "text/csv")}


@pytest.fixture
def api_client(db_engine):
    """TestClient backed by a fresh Postgres — the same DB the import
    endpoint normalizes against (it builds its own pipeline from
    ``settings.database_url``, which the patched env points here)."""
    from src.core.db import get_db

    def _override_get_db():
        with Session(db_engine) as session:
            yield session

    db_url = db_engine.url.render_as_string(hide_password=False)
    with patch.dict(os.environ, {"API_TOKEN": "test-secret", "DATABASE_URL": db_url}):
        from src.api.app import create_app

        app = create_app()
        app.dependency_overrides[get_db] = _override_get_db
        client = TestClient(app)
        yield client
        app.dependency_overrides.clear()


def test_import_requires_auth(api_client):
    resp = api_client.post(
        "/api/v1/import/paypal-csv", files=_upload(_make_csv([_PAYMENT_A]))
    )
    assert resp.status_code == 401


def test_import_skips_funding_rows_and_normalizes(api_client):
    """The funding row is skipped; the two real payments are ingested and
    normalized in one call."""
    resp = api_client.post(
        "/api/v1/import/paypal-csv",
        files=_upload(_make_csv([_PAYMENT_A, _FUNDING, _PAYMENT_B])),
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "parsed": 2,  # _FUNDING skipped — empty Name
        "inserted": 2,
        "duplicates": 0,
        "normalized": 2,
    }


def test_reupload_is_idempotent(api_client):
    """Re-uploading an overlapping export inserts nothing the second time
    — (source, external_id) dedup absorbs it."""
    content = _make_csv([_PAYMENT_A, _PAYMENT_B])
    first = api_client.post(
        "/api/v1/import/paypal-csv", files=_upload(content), headers=AUTH
    )
    assert first.status_code == 200 and first.json()["inserted"] == 2

    second = api_client.post(
        "/api/v1/import/paypal-csv", files=_upload(content), headers=AUTH
    )
    assert second.status_code == 200
    assert second.json() == {
        "parsed": 2,
        "inserted": 0,
        "duplicates": 2,
        "normalized": 2,
    }


def test_malformed_csv_returns_422(api_client):
    """A CSV missing a required column fails with an actionable 422."""
    columns = [c for c in _COLUMNS if c != "Transaktionscode"]
    row = [v for c, v in zip(_COLUMNS, _PAYMENT_A) if c != "Transaktionscode"]
    resp = api_client.post(
        "/api/v1/import/paypal-csv",
        files=_upload(_make_csv([row], columns)),
        headers=AUTH,
    )
    assert resp.status_code == 422
    assert "Transaktionscode" in resp.json()["detail"]


def test_empty_file_returns_400(api_client):
    resp = api_client.post(
        "/api/v1/import/paypal-csv", files=_upload(b""), headers=AUTH
    )
    assert resp.status_code == 400
