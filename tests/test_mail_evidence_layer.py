from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.core.db.models import (
    Base,
    Budget,
    Category,
    DataSource,
    NormalizedTransaction,
    RawTransaction,
    TypeEnum,
)
from src.services import financial_aggregates
from src.services.llm_context import assert_context_safe, sanitize_context, sanitize_search_query
from src.services.mail_evidence import extract_evidence_from_message, import_mail_message

AUTH = {"Authorization": "Bearer test-secret"}


@pytest.fixture
def sqlite_engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def sqlite_api_client(sqlite_engine):
    from src.core.db import get_db

    def _override_get_db():
        with Session(sqlite_engine) as session:
            yield session

    with patch.dict(os.environ, {"API_TOKEN": "test-secret", "DATABASE_URL": ""}):
        from src.api.app import create_app

        app = create_app()
        app.dependency_overrides[get_db] = _override_get_db
        client = TestClient(app)
        yield client
        app.dependency_overrides.clear()


def _seed_purchase(session: Session) -> None:
    session.add(
        Category(
            id="shopping",
            name="Shopping",
            type=TypeEnum.DISKRETIONAER,
            kind="expense",
            budgetable=True,
            analysis_group="discretionary",
            examples=["retail orders"],
            anti_examples=["salary"],
            llm_hints="Use receipt line items when available.",
        )
    )
    session.add(
        Budget(
            category_id="shopping",
            monthly_limit=Decimal("40.00"),
            priority=2,
            warning_threshold=Decimal("0.80"),
            critical_threshold=Decimal("1.00"),
            context_note="Keep discretionary orders tight.",
        )
    )
    session.add(
        RawTransaction(
            content_hash="m" * 64,
            source=DataSource.PAYPAL,
            external_id="paypal-mock-001",
            raw_data={"stub": True},
        )
    )
    session.flush()
    session.add(
        NormalizedTransaction(
            id="txn-mail-match-001",
            raw_content_hash="m" * 64,
            source=DataSource.PAYPAL,
            external_id="paypal-mock-001",
            booking_date=date(2026, 6, 7),
            valuation_date=date(2026, 6, 7),
            amount=Decimal("-42.99"),
            currency="EUR",
            sender="Max Mustermann",
            recipient="PayPal *ACME Shop",
            description="ACME Shop Paypal checkout",
            category_id="shopping",
            is_recurring=False,
            is_outlier=False,
            internal_transfer=False,
        )
    )
    session.commit()


def _mock_mail_payload(message_id: str = "gmail-msg-demo-001") -> dict:
    return {
        "source": "gmail",
        "source_message_id": message_id,
        "sender": "receipts@acme.example",
        "subject": "Invoice for order ACME-123456789",
        "body_text": "\n".join(
            [
                "Merchant: ACME Shop GmbH",
                "Date: 2026-06-07",
                "Total: 42,99 EUR",
                "Order: ACME-123456789",
                "Payment: PayPal",
                "Customer email: max@example.invalid",
                "IBAN: DE89370400440532013000",
                "- Outdoor Jacket | 39,99 | clothing",
                "- Shipping | 3,00 | shipping",
            ]
        ),
    }


def test_mock_mail_evidence_is_redacted_and_matched(sqlite_engine):
    with Session(sqlite_engine) as session:
        _seed_purchase(session)

        evidence, links = import_mail_message(session, _mock_mail_payload())

        assert evidence.total_amount == Decimal("42.99")
        assert evidence.merchant_key == "acme shop"
        assert evidence.order_ref_hash is not None
        assert "ACME-123456789" not in (evidence.subject_hint or "")
        assert "ACME-123456789" not in (evidence.redacted_snippet or "")
        assert "max@example.invalid" not in (evidence.redacted_snippet or "")
        assert "DE89370400440532013000" not in (evidence.redacted_snippet or "")
        assert evidence.line_items == [
            {
                "label": "Outdoor Jacket",
                "amount": "39.99",
                "category_hint": "clothing",
            },
            {"label": "Shipping", "amount": "3.00", "category_hint": "shipping"},
        ]

        assert len(links) == 1
        assert links[0].transaction_id == "txn-mail-match-001"
        assert links[0].match_type == "invoice_match"
        assert links[0].confidence >= Decimal("0.90")


def test_analysis_context_includes_budget_and_mail_evidence(sqlite_engine):
    with Session(sqlite_engine) as session:
        _seed_purchase(session)
        evidence, _links = import_mail_message(
            session,
            {
                "source": "gmail",
                "source_message_id": "gmail-msg-demo-002",
                "sender": "receipts@acme.example",
                "subject": "ACME receipt",
                "body_text": "\n".join(
                    [
                        "Merchant: ACME Shop",
                        "Date: 2026-06-07",
                        "Total: 42.99 EUR",
                        "Payment: PayPal",
                    ]
                ),
            },
        )

        context = financial_aggregates.analysis_context(session, year=2026, month=6)

    assert context["budget_risks"][0]["category_id"] == "shopping"
    assert context["budget_risks"][0]["status"] == "critical"
    assert context["mail_evidence"][0]["id"] == evidence.id
    assert context["top_transactions"][0]["evidence"]["id"] == evidence.id


def test_mail_evidence_api_import_lists_and_feeds_analysis_context(
    sqlite_engine,
    sqlite_api_client,
):
    with Session(sqlite_engine) as session:
        _seed_purchase(session)

    import_resp = sqlite_api_client.post(
        "/api/v1/mail-evidence/mock-import",
        headers=AUTH,
        json=_mock_mail_payload(),
    )
    assert import_resp.status_code == 200
    imported = import_resp.json()
    assert imported["evidence"]["total_amount"] == "42.99"
    assert imported["evidence"]["order_ref_hash"]
    assert "ACME-123456789" not in imported["evidence"]["subject_hint"]
    assert "max@example.invalid" not in imported["evidence"]["redacted_snippet"]
    assert imported["links"][0]["transaction_id"] == "txn-mail-match-001"

    list_resp = sqlite_api_client.get("/api/v1/mail-evidence", headers=AUTH)
    assert list_resp.status_code == 200
    assert list_resp.json()[0]["id"] == imported["evidence"]["id"]

    context_resp = sqlite_api_client.get(
        "/api/v1/aggregates/analysis-context?year=2026&month=6",
        headers=AUTH,
    )
    assert context_resp.status_code == 200
    context = context_resp.json()
    assert context["budget_risks"][0]["category_id"] == "shopping"
    assert context["top_transactions"][0]["evidence"]["id"] == imported["evidence"]["id"]


def test_llm_context_sanitizer_redacts_and_pseudonymizes_sensitive_fields():
    sanitized = sanitize_context(
        {
            "transaction_id": "txn-real-001",
            "transaction_ids": ["txn-real-001", "txn-real-002"],
            "external_id": "provider-secret-id",
            "source_payload": {"raw": "do not send"},
            "order_id": 123456789,
            "invoice_number": "INV-123456789",
            "description": (
                "Invoice ACME-123456789 for max@example.invalid "
                "IBAN DE89370400440532013000"
            ),
            "nested": {"evidence_id": "mail_real_001"},
        }
    )

    assert sanitized["transaction_id"] == "tx_001"
    assert sanitized["transaction_ids"] == ["tx_001", "tx_002"]
    assert "external_id" not in sanitized
    assert "source_payload" not in sanitized
    assert "order_id" not in sanitized
    assert "invoice_number" not in sanitized
    assert "max@example.invalid" not in sanitized["description"]
    assert "DE89370400440532013000" not in sanitized["description"]
    assert "ACME-123456789" not in sanitized["description"]
    assert sanitized["nested"]["evidence_id"] == "ev_001"


def test_search_query_sanitizer_removes_private_reference_noise():
    query = sanitize_search_query(
        "ACME Shop order ACME-123456789 max@example.invalid 2026-06-07"
    )

    assert "ACME Shop" in query
    assert "ACME-123456789" not in query
    assert "max@example.invalid" not in query


def test_context_safety_rejects_unsanitized_reference_patterns():
    with pytest.raises(ValueError, match="forbidden pattern"):
        assert_context_safe({"description": "Order ACME-123456789"})


@pytest.mark.parametrize(
    ("subject", "expected_type"),
    [
        ("Receipt from ACME Shop", "receipt"),
        ("Invoice from ACME Shop", "invoice"),
        ("Subscription renewal ACME Cloud", "subscription"),
        ("Refund for ACME return", "refund"),
        ("Order confirmation ACME Shop", "order"),
    ],
)
def test_mail_evidence_type_detection_covers_planned_mail_objects(
    subject: str,
    expected_type: str,
):
    draft = extract_evidence_from_message(
        {
            "source_message_id": f"msg-{expected_type}",
            "sender": "receipts@acme.example",
            "subject": subject,
            "body_text": "Merchant: ACME Shop\nTotal: 1.00 EUR\nDate: 2026-06-07",
        }
    )

    assert draft["evidence_type"] == expected_type
