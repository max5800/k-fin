"""Trustworthy analytics regression contract (accounting/report v2)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.core.db.models import (
    Category,
    Base,
    AnalyticsCorrectionRun,
    DataSource,
    MailEvidence,
    NormalizedTransaction,
    RawTransaction,
    SourceStatementPeriod,
    SubscriptionRecord,
    TransactionLink,
    TypeEnum,
    ValueAssessment,
)
from src.agents.gather import get_uncategorized_transactions
from src.normalization.pipeline import NormalizationPipeline
from src.services.analytics_correction import run_correction
from src.services.mail_evidence import match_evidence_to_transactions
from src.services import trustworthy_analytics
from src.services.trustworthy_analytics import (
    SUBSCRIPTION_STATUSES,
    accounting_report,
    monthly_review,
    subscriptions,
)


@pytest.fixture
def analytics_engine():
    """Portable service-level database; PostgreSQL migration tests run separately."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def analytics_api():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    from src.core.db import get_db
    from src.api.deps import get_report_db

    def override_db():
        with Session(engine) as db:
            yield db

    with patch.dict(
        "os.environ",
        {
            "API_TOKEN": "test-secret",
            "DATABASE_URL": "",
            "JWT_SECRET": "integration-test-secret-minimum-32-chars!!",
        },
    ):
        from src.api.app import create_app

        app = create_app()
        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_report_db] = override_db
        yield TestClient(app), engine
        app.dependency_overrides.clear()
    engine.dispose()


def _raw(char: str, *, superseded_by: str | None = None) -> RawTransaction:
    return RawTransaction(
        content_hash=char * 64,
        source=DataSource.COMDIRECT,
        raw_data={"stub": True},
        superseded_by=superseded_by,
    )


def _tx(
    tx_id: str,
    raw_char: str,
    amount: str,
    *,
    accounting_class: str = "unresolved_ambiguous",
    confidence: str = "0.000",
    category_id: str | None = None,
    active: bool = True,
    refund: bool = False,
    refund_decided: bool = False,
) -> NormalizedTransaction:
    return NormalizedTransaction(
        id=tx_id,
        raw_content_hash=raw_char * 64,
        booking_date=date(2026, 1, 15),
        valuation_date=date(2026, 1, 15),
        amount=Decimal(amount),
        currency="EUR",
        category_id=category_id,
        is_active=active,
        accounting_class=accounting_class,
        accounting_confidence=Decimal(confidence),
        is_refund=refund,
        refund_verification_status="user_verified" if refund_decided else "unverified",
        refund_audit_decided_at=(
            datetime(2026, 1, 20, tzinfo=timezone.utc) if refund_decided else None
        ),
    )


def _verified_periods() -> list[SourceStatementPeriod]:
    return [
        SourceStatementPeriod(
            id=f"period-{source.value}",
            source=source,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
            verified_complete=True,
        )
        for source in DataSource
    ]


def test_stale_normalized_predecessor_is_superseded_idempotently(analytics_engine):
    with Session(analytics_engine) as db:
        db.add(_raw("a", superseded_by="b" * 64))
        db.add(_raw("b"))
        db.add(_tx("old", "a", "-10.00"))
        db.add(_tx("new", "b", "-12.00"))
        db.add(
            TransactionLink(
                id="stale-link",
                parent_transaction_id="old",
                child_transaction_id="new",
                link_type="paypal_aggregate",
            )
        )
        db.add(
            TransactionLink(
                id="stale-manual-link",
                parent_transaction_id="old",
                child_transaction_id="new",
                link_type="manual",
            )
        )
        db.commit()

        first = run_correction(db, apply=True)
        assert first["result_counts"]["stale_normalized_to_supersede"] == 1
        assert first["result_counts"]["invalid_links_to_deactivate"] == 2
        assert db.get(NormalizedTransaction, "old").normalization_status == "superseded"
        assert db.get(NormalizedTransaction, "old").is_active is False
        assert db.get(NormalizedTransaction, "old").superseded_by_id == "b" * 64
        assert db.get(TransactionLink, "stale-link").status == "invalid_participant"
        assert db.get(TransactionLink, "stale-manual-link").status == "invalid_participant"

        second = run_correction(db, apply=True)
        assert second["result_counts"]["total_changes"] == 0
        assert second["applied"] is False
        assert db.query(AnalyticsCorrectionRun).count() == 1


def test_correction_reactivates_and_refreshes_in_one_apply(analytics_engine):
    with Session(analytics_engine) as db:
        db.add(_raw("a"))
        db.add(_tx("current", "a", "-12.00", category_id="groceries", active=False))
        db.commit()

        dry_run = run_correction(db)
        assert dry_run["result_counts"]["current_normalized_to_reactivate"] == 1
        assert dry_run["result_counts"]["accounting_classifications_to_refresh"] == 1
        assert db.get(NormalizedTransaction, "current").is_active is False

        applied = run_correction(db, apply=True)
        current = db.get(NormalizedTransaction, "current")
        assert applied["result_counts"]["total_changes"] == 2
        assert current.is_active is True
        assert current.accounting_class == "reconciled_consumption"
        assert current.accounting_version == 2

        second = run_correction(db, apply=True)
        assert second["result_counts"]["total_changes"] == 0
        assert db.query(AnalyticsCorrectionRun).count() == 1


def test_ambiguous_legacy_link_is_re_evaluated_and_fails_closed(
    db_engine,
):
    with Session(db_engine) as db:
        db.add_all([_raw("a"), _raw("b"), _raw("c")])
        db.flush()
        parent = _tx(
            "legacy-parent",
            "a",
            "-10.00",
            accounting_class="internal_transfer_settlement_parent",
            confidence="0.900",
        )
        parent.source = DataSource.COMDIRECT
        parent.recipient = "PayPal"
        parent.description = "PayPal settlement"
        parent.internal_transfer = True
        child_a = _tx("detail-a", "b", "-10.00")
        child_b = _tx("detail-b", "c", "-10.00")
        child_a.source = child_b.source = DataSource.PAYPAL
        db.add_all([parent, child_a, child_b])
        db.flush()
        db.add(
            TransactionLink(
                id="legacy-ambiguous-link",
                parent_transaction_id=parent.id,
                child_transaction_id=child_a.id,
                link_type="paypal_aggregate",
            )
        )
        db.commit()

        result = run_correction(db, apply=True)

        link = db.get(TransactionLink, "legacy-ambiguous-link")
        assert result["result_counts"]["invalid_links_to_deactivate"] == 1
        assert link.is_active is False
        assert link.status == "unverified_match"
        assert link.version == 2
        assert parent.internal_transfer is False
        assert parent.accounting_class == "unresolved_ambiguous"
        assert parent.accounting_confidence == Decimal("0.000")


def test_bank_card_paypal_chain_is_linked_and_counted_once():
    frame = pd.DataFrame(
        [
            {
                "id": "merchant",
                "source": DataSource.PAYPAL,
                "amount": Decimal("-40.00"),
                "booking_date": "2026-01-10",
                "description": "Dummy merchant",
                "sender": None,
                "recipient": "Dummy merchant",
                "sender_iban": None,
                "recipient_iban": None,
                "internal_transfer": False,
            },
            {
                "id": "card-parent",
                "source": DataSource.SANTANDER_CC,
                "amount": Decimal("-40.00"),
                "booking_date": "2026-01-10",
                "description": "PayPal",
                "sender": None,
                "recipient": "PayPal",
                "sender_iban": None,
                "recipient_iban": None,
                "internal_transfer": False,
            },
            {
                "id": "bank-parent",
                "source": DataSource.COMDIRECT,
                "amount": Decimal("-40.00"),
                "booking_date": "2026-02-01",
                "description": "Kartenabrechnung",
                "sender": None,
                "recipient": "Santander",
                "sender_iban": None,
                "recipient_iban": None,
                "internal_transfer": False,
            },
        ]
    )
    reconciled, links = NormalizationPipeline._reconcile_cross_source_transfers(frame)
    flags = dict(zip(reconciled["id"], reconciled["internal_transfer"]))
    assert flags == {"merchant": False, "card-parent": True, "bank-parent": True}
    assert {
        (link["parent_transaction_id"], link["child_transaction_id"])
        for link in links
    } == {("card-parent", "merchant"), ("bank-parent", "card-parent")}


def test_bank_card_paypal_chain_report_arithmetic_counts_merchant_once(
    analytics_engine,
):
    with Session(analytics_engine) as db:
        db.add_all([_raw(char) for char in "abc"])
        bank = _tx(
            "bank-parent",
            "a",
            "-40.00",
            accounting_class="internal_transfer_settlement_parent",
            confidence="1.000",
        )
        card = _tx(
            "card-parent",
            "b",
            "-40.00",
            accounting_class="internal_transfer_settlement_parent",
            confidence="1.000",
        )
        merchant = _tx(
            "merchant",
            "c",
            "-40.00",
            accounting_class="reconciled_consumption",
            confidence="0.800",
        )
        bank.internal_transfer = card.internal_transfer = True
        db.add_all([bank, card, merchant])
        db.commit()

        report = accounting_report(db, start=date(2026, 1, 1), end=date(2026, 1, 31))
        assert report["report_version"] == 2
        assert report["gross_cash_outflow"] == Decimal("40.00")
        assert report["reconciled_consumption_gross"] == Decimal("40.00")
        assert report["internal_transfer_and_settlement_parent_outflow"] == Decimal(
            "80.00"
        )


def test_partial_source_period_blocks_monthly_review(analytics_engine):
    with Session(analytics_engine) as db:
        db.add(
            SourceStatementPeriod(
                id="period-1",
                source=DataSource.COMDIRECT,
                period_start=date(2026, 1, 1),
                period_end=date(2026, 1, 31),
                rows_present=True,
                observed_row_count=3,
                verified_complete=False,
            )
        )
        db.commit()
        result = monthly_review(db, year=2026, month=1)
        assert result["state"] == "missing_source_periods"
        assert result["facts"] is None
        assert result["source_completeness"]["missing_periods"] == [
            {
                "source": "comdirect",
                "period_start": "2026-01-01",
                "period_end": "2026-01-31",
                "reason": "rows_present_unverified",
            },
            {
                "source": "paypal",
                "period_start": "2026-01-01",
                "period_end": "2026-01-31",
                "reason": "missing",
            },
            {
                "source": "santander_cc",
                "period_start": "2026-01-01",
                "period_end": "2026-01-31",
                "reason": "missing",
            },
        ]


def test_observed_source_cannot_be_omitted_by_narrow_server_policy(
    analytics_engine,
):
    with Session(analytics_engine) as db:
        db.add(_raw("a"))
        tx = _tx("paypal-row", "a", "-10.00")
        tx.source = DataSource.PAYPAL
        db.add(tx)
        db.add(
            SourceStatementPeriod(
                id="comdirect-only",
                source=DataSource.COMDIRECT,
                period_start=date(2026, 1, 1),
                period_end=date(2026, 1, 31),
                verified_complete=True,
            )
        )
        db.commit()

        with patch.object(
            trustworthy_analytics.settings,
            "analytics_required_sources",
            "comdirect",
        ):
            result = monthly_review(db, year=2026, month=1)

        assert result["can_analyze"] is False
        assert result["source_completeness"]["missing_periods"] == [
            {
                "source": "paypal",
                "period_start": "2026-01-01",
                "period_end": "2026-01-31",
                "reason": "missing",
            }
        ]


def test_monthly_review_api_exposes_ui_gate_and_disabled_scheduler(analytics_api):
    client, _engine = analytics_api
    response = client.get(
        "/api/v1/analytics/v2/monthly-review?year=2026&month=1",
        headers={"Authorization": "Bearer test-secret"},
    )
    assert response.status_code == 200
    assert response.json()["state"] == "missing_source_periods"
    assert response.json()["can_analyze"] is False
    assert response.json()["scheduler_enabled"] is False
    assert response.json()["workflow_version"] == 2
    assert [
        item["source"] for item in response.json()["source_completeness"]["missing_periods"]
    ] == ["comdirect", "paypal", "santander_cc"]


def test_ambiguous_paypal_link_stays_unlinked_and_residual(analytics_engine):
    frame = pd.DataFrame(
        [
            {
                "id": "parent",
                "source": DataSource.COMDIRECT,
                "amount": Decimal("-10.00"),
                "booking_date": "2026-01-10",
                "description": "PayPal",
                "recipient": "PayPal",
                "sender": None,
                "sender_iban": None,
                "recipient_iban": None,
                "internal_transfer": False,
            },
            *[
                {
                    "id": f"child-{index}",
                    "source": DataSource.PAYPAL,
                    "amount": Decimal("-10.00"),
                    "booking_date": "2026-01-10",
                    "description": "Dummy merchant",
                    "recipient": "Dummy merchant",
                    "sender": None,
                    "sender_iban": None,
                    "recipient_iban": None,
                    "internal_transfer": False,
                }
                for index in (1, 2)
            ],
        ]
    )
    reconciled, links = NormalizationPipeline._reconcile_cross_source_transfers(frame)
    assert links == []
    assert not reconciled["internal_transfer"].any()

    with Session(analytics_engine) as db:
        db.add(_raw("a"))
        parent = _tx("parent", "a", "-10.00")
        parent.description = "PayPal"
        parent.recipient = "PayPal"
        db.add(parent)
        db.commit()
        run_correction(db, apply=True)
        report = accounting_report(db, start=date(2026, 1, 1), end=date(2026, 1, 31))
        assert report["unresolved_ambiguous_outflow_residual"] == Decimal("10.00")
        assert report["confidence"] == "low"


def test_two_indistinguishable_paypal_topups_stay_unlinked():
    frame = pd.DataFrame(
        [
            {
                "id": "bank-parent",
                "source": DataSource.COMDIRECT,
                "amount": Decimal("-10.00"),
                "booking_date": "2026-01-10",
                "description": "PayPal",
                "recipient": "PayPal",
                "sender": None,
                "sender_iban": None,
                "recipient_iban": None,
                "internal_transfer": False,
            },
            *[
                {
                    "id": f"deposit-{index}",
                    "source": DataSource.PAYPAL,
                    "amount": Decimal("10.00"),
                    "booking_date": "2026-01-10",
                    "description": "bankgutschrift auf paypal-konto",
                    "recipient": None,
                    "sender": None,
                    "sender_iban": None,
                    "recipient_iban": None,
                    "internal_transfer": False,
                }
                for index in (1, 2)
            ],
        ]
    )
    reconciled, links = NormalizationPipeline._reconcile_cross_source_transfers(frame)
    assert links == []
    assert not reconciled["internal_transfer"].any()


def test_report_separates_refunds_assets_debt_and_legacy_residual(analytics_engine):
    with Session(analytics_engine) as db:
        db.add_all([_raw(char) for char in "abcde"])
        db.add_all(
            [
                _tx("consume", "a", "-50.00", accounting_class="reconciled_consumption", confidence="0.800"),
                _tx("asset", "b", "-25.00", accounting_class="financial_asset_building", confidence="0.950"),
                _tx("debt", "c", "-15.00", accounting_class="debt_principal_financing", confidence="0.850"),
                _tx("refund", "d", "10.00", accounting_class="verified_refund_reimbursement", confidence="1.000", refund=True, refund_decided=True),
                _tx("legacy", "e", "-5.00"),
            ]
        )
        db.commit()
        report = accounting_report(db, start=date(2026, 1, 1), end=date(2026, 1, 31))
        assert report["gross_cash_outflow"] == Decimal("95.00")
        assert report["financial_asset_building_outflow"] == Decimal("25.00")
        assert report["distinguishable_debt_principal_financing_outflow"] == Decimal("15.00")
        assert report["verified_refunds_reimbursements"] == Decimal("10.00")
        assert report["reconciled_consumption_net"] == Decimal("40.00")
        assert report["unresolved_ambiguous_outflow_residual"] == Decimal("5.00")


def test_ambiguous_positive_and_negative_refund_fail_closed(analytics_engine):
    with Session(analytics_engine) as db:
        db.add_all([_raw("a"), _raw("b")])
        positive = _tx("positive", "a", "10.00")
        negative = _tx(
            "negative",
            "b",
            "-10.00",
            refund=True,
            refund_decided=True,
        )
        db.add_all([positive, negative])
        db.commit()

        run_correction(db, apply=True)
        assert positive.accounting_class == "unresolved_ambiguous"
        assert positive.accounting_confidence == Decimal("0.000")
        assert negative.accounting_class == "unresolved_ambiguous"
        report = accounting_report(db, start=date(2026, 1, 1), end=date(2026, 1, 31))
        assert report["verified_refunds_reimbursements"] == Decimal("0.00")
        assert report["unresolved_ambiguous_inflow_residual"] == Decimal("10.00")
        assert report["unresolved_ambiguous_outflow_residual"] == Decimal("10.00")


def test_agent_gather_and_mail_matching_ignore_inactive_rows(analytics_engine):
    with Session(analytics_engine) as db:
        db.add_all([_raw("a"), _raw("b")])
        active = _tx("active", "a", "-10.00")
        inactive = _tx("inactive", "b", "-10.00", active=False)
        active.recipient = inactive.recipient = "Dummy Merchant"
        db.add_all([active, inactive])
        evidence = MailEvidence(
            id="evidence-1",
            source="gmail",
            evidence_type="receipt",
            merchant_name="Dummy Merchant",
            merchant_key="dummy merchant",
            document_date=date(2026, 1, 15),
            total_amount=Decimal("10.00"),
            currency="EUR",
            confidence=Decimal("0.900"),
        )
        db.add(evidence)
        db.commit()

        links = match_evidence_to_transactions(db, evidence)
        assert [link.transaction_id for link in links] == ["active"]
        db.rollback()

    gathered = get_uncategorized_transactions(analytics_engine)
    assert [item["id"] for item in gathered] == ["active"]


def test_api_rejects_negative_verified_refund(analytics_api):
    client, engine = analytics_api
    with Session(engine) as db:
        db.add(_raw("a"))
        db.add(_tx("negative", "a", "-10.00"))
        db.commit()

    response = client.patch(
        "/api/v1/transactions/negative",
        json={"is_refund": True},
        headers={"Authorization": "Bearer test-secret"},
    )
    assert response.status_code == 422


def test_refund_decision_and_reversal_refresh_income_accounting(analytics_api):
    client, engine = analytics_api
    with Session(engine) as db:
        db.add(_raw("a"))
        db.add(_tx("income-decision", "a", "25.00"))
        db.commit()

    confirmed = client.patch(
        "/api/v1/transactions/income-decision",
        json={"refund_audit_decided": True},
        headers={"Authorization": "Bearer test-secret"},
    )
    assert confirmed.status_code == 200
    with Session(engine) as db:
        tx = db.get(NormalizedTransaction, "income-decision")
        assert tx.refund_verification_status == "income_verified"
        assert tx.accounting_class == "non_outflow_income"
        assert tx.accounting_confidence == Decimal("1.000")

    reversed_decision = client.patch(
        "/api/v1/transactions/income-decision",
        json={"refund_audit_decided": False},
        headers={"Authorization": "Bearer test-secret"},
    )
    assert reversed_decision.status_code == 200
    with Session(engine) as db:
        tx = db.get(NormalizedTransaction, "income-decision")
        assert tx.refund_verification_status == "unverified"
        assert tx.accounting_class == "unresolved_ambiguous"
        assert tx.accounting_confidence == Decimal("0.000")


def test_legacy_category_is_backfilled_without_guessing_uncategorized(analytics_engine):
    with Session(analytics_engine) as db:
        db.add(Category(id="etf-sparplan", name="ETF-Sparplan", type=TypeEnum.FIX))
        db.add_all([_raw("a"), _raw("b")])
        db.add(_tx("known", "a", "-20.00", category_id="etf-sparplan"))
        db.add(_tx("unknown", "b", "-20.00"))
        db.commit()
        run_correction(db, apply=True)
        assert db.get(NormalizedTransaction, "known").accounting_class == "financial_asset_building"
        assert db.get(NormalizedTransaction, "unknown").accounting_class == "unresolved_ambiguous"


def test_subscription_statuses_are_itemized_discrete_scenarios(analytics_engine):
    with Session(analytics_engine) as db:
        for index, status in enumerate(SUBSCRIPTION_STATUSES):
            db.add(
                SubscriptionRecord(
                    id=f"subscription-{index}",
                    label=f"Dummy service {index}",
                    status=status,
                    confidence=Decimal("0.750"),
                    evidence_source="manual_review",
                    amount_scenarios=["5.00", "8.00"],
                )
            )
        db.commit()
        items = subscriptions(db, start=date(2026, 1, 1), end=date(2026, 1, 31))
        assert {item["status"] for item in items} == set(SUBSCRIPTION_STATUSES)
        assert all(
            item["scenario_semantics"] == "discrete_scenarios_not_range_or_contract_truth"
            for item in items
        )


def test_high_impact_ambiguous_value_becomes_question(analytics_engine):
    with Session(analytics_engine) as db:
        db.add(_raw("a"))
        db.add(_tx("purchase", "a", "-150.00"))
        db.add(
            ValueAssessment(
                id="assessment-1",
                transaction_id="purchase",
                value_class="convenience",
                confidence=Decimal("0.400"),
                declared_priority=None,
                observed_use_count=None,
                question="Was this aligned with a declared priority?",
            )
        )
        db.add_all(_verified_periods())
        db.commit()
        result = monthly_review(db, year=2026, month=1)
        assert result["state"] == "analysis_ready"
        assert result["high_impact_questions"][0]["assessment_id"] == "assessment-1"
        assert result["value_review"]["objective"] == "less_waste_and_more_priority_aligned_value"


def test_high_impact_without_value_assessment_becomes_question(analytics_engine):
    with Session(analytics_engine) as db:
        db.add(_raw("a"))
        db.add(_tx("unassessed", "a", "-150.00"))
        db.add_all(_verified_periods())
        db.commit()

        result = monthly_review(db, year=2026, month=1)
        assert result["state"] == "analysis_ready"
        assert result["high_impact_questions"] == [
            {
                "transaction_id": "unassessed",
                "question": "How did this high-impact transaction support a declared priority?",
                "reason": "high_impact_missing_assessment",
            }
        ]
