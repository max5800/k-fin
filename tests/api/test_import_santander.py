"""Tests for the Santander statement-PDF import endpoint (POST /import/santander-pdf).

The PDF parsing itself is covered by ``tests/test_santander_pdf.py``; here
``parse_statement`` is patched to return controlled transaction dicts, so the
tests exercise the endpoint — multi-file upload, per-file error aggregation,
ingest + normalize, idempotent re-upload, the auth guard — against a
throw-away Postgres (testcontainers).
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from src.normalization.santander_pdf import SantanderPdfError

AUTH = {"Authorization": "Bearer test-secret"}


def _txn(*, transaction_id: str, amount: str, merchant: str) -> dict:
    """One ``SantanderTransaction``-shaped dict — the shape parse_statement yields."""
    return {
        "transaction_id": transaction_id,
        "booking_date": "2026-05-04",
        "valuation_date": "2026-05-02",
        "merchant_name": merchant,
        "merchant_country": None,
        "amount": amount,
        "currency": "EUR",
        "original_amount": None,
        "original_currency": None,
        "card_last4": "0000",
        "pending": False,
    }


_STMT_A = [
    _txn(transaction_id="santander-pdf-a1", amount="-40.00", merchant="Shop A"),
    _txn(transaction_id="santander-pdf-a2", amount="-10.00", merchant="Shop B"),
]
_STMT_B = [
    _txn(transaction_id="santander-pdf-b1", amount="-5.00", merchant="Shop C"),
]


def _fake_parse(source: bytes) -> list[dict]:
    """Stand-in for parse_statement — maps upload bytes to dummy transactions."""
    raw = bytes(source)
    if raw == b"BAD-PDF":
        raise SantanderPdfError("not a recognisable 1plus-Card statement")
    return {b"STMT-A": _STMT_A, b"STMT-B": _STMT_B}.get(raw, [])


def _files(*contents: bytes) -> list:
    """Multipart ``files`` field — one part per uploaded statement PDF."""
    return [
        ("files", (f"statement-{i}.pdf", content, "application/pdf"))
        for i, content in enumerate(contents)
    ]


@pytest.fixture
def api_client(db_engine):
    """TestClient backed by a fresh Postgres (mirrors tests/api/test_import_csv.py)."""
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
    with patch("src.api.routers.import_csv.parse_statement", _fake_parse):
        resp = api_client.post(
            "/api/v1/import/santander-pdf", files=_files(b"STMT-A")
        )
    assert resp.status_code == 401


def test_import_multiple_statements(api_client):
    """Two statements upload in one call — every transaction ingested + normalized."""
    with patch("src.api.routers.import_csv.parse_statement", _fake_parse):
        resp = api_client.post(
            "/api/v1/import/santander-pdf",
            files=_files(b"STMT-A", b"STMT-B"),
            headers=AUTH,
        )
    assert resp.status_code == 200
    assert resp.json() == {
        "statements": 2,
        "parsed": 3,
        "inserted": 3,
        "duplicates": 0,
        "normalized": 3,
        "errors": [],
    }


def test_unreadable_file_is_reported_not_fatal(api_client):
    """A bad PDF lands in ``errors``; the good statement still imports."""
    with patch("src.api.routers.import_csv.parse_statement", _fake_parse):
        resp = api_client.post(
            "/api/v1/import/santander-pdf",
            files=_files(b"STMT-A", b"BAD-PDF"),
            headers=AUTH,
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["statements"] == 1
    assert body["parsed"] == 2
    assert body["inserted"] == 2
    assert len(body["errors"]) == 1
    assert "1plus-Card statement" in body["errors"][0]


def test_all_files_unreadable_returns_422(api_client):
    with patch("src.api.routers.import_csv.parse_statement", _fake_parse):
        resp = api_client.post(
            "/api/v1/import/santander-pdf", files=_files(b"BAD-PDF"), headers=AUTH
        )
    assert resp.status_code == 422


def test_reupload_is_idempotent(api_client):
    """Re-uploading a statement inserts nothing the second time."""
    with patch("src.api.routers.import_csv.parse_statement", _fake_parse):
        first = api_client.post(
            "/api/v1/import/santander-pdf", files=_files(b"STMT-A"), headers=AUTH
        )
        second = api_client.post(
            "/api/v1/import/santander-pdf", files=_files(b"STMT-A"), headers=AUTH
        )
    assert first.status_code == 200 and first.json()["inserted"] == 2
    assert second.status_code == 200
    assert second.json()["inserted"] == 0
    assert second.json()["duplicates"] == 2


def test_empty_file_is_reported(api_client):
    """An empty upload is reported; a good statement alongside still imports."""
    with patch("src.api.routers.import_csv.parse_statement", _fake_parse):
        resp = api_client.post(
            "/api/v1/import/santander-pdf",
            files=_files(b"", b"STMT-A"),
            headers=AUTH,
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["statements"] == 1
    assert body["inserted"] == 2
    assert any("empty" in err for err in body["errors"])
