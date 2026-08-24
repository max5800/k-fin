"""Refund-aware aggregation tests.

Pins the contract introduced 2026-05-08:
  - `is_refund=True` positive amounts are folded into expenses, not income.
  - Budget consumption per category is `spent_gross + refunded`.
  - `internal_transfer=True` rows still don't show up in either bucket.
"""

from __future__ import annotations

import os
import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from src.core.db.models import (
    Budget,
    Category,
    NormalizedTransaction,
    RawTransaction,
    TypeEnum,
    User,
)

AUTH = {"Authorization": "Bearer test-secret"}
JWT_SECRET = "integration-test-secret-minimum-32-chars!!"


@pytest.fixture
def api_client(db_engine):
    from src.core.db import get_db

    def _override_get_db():
        with Session(db_engine) as session:
            yield session

    db_url = db_engine.url.render_as_string(hide_password=False)
    with patch.dict(
        os.environ,
        {
            "API_TOKEN": "test-secret",
            "DATABASE_URL": db_url,
            "JWT_SECRET": JWT_SECRET,
        },
    ):
        from src.api.app import create_app

        app = create_app()
        app.dependency_overrides[get_db] = _override_get_db
        client = TestClient(app)
        yield client
        app.dependency_overrides.clear()


@pytest.fixture
def user_auth(db_engine):
    """Seed a user and return a JWT bearer header.

    Used by tests that hit user-only endpoints (e.g. refund-audit
    auto-apply) — the static API_TOKEN must be rejected there.
    """
    from src.api.auth.passwords import hash_password

    with patch.dict(os.environ, {"JWT_SECRET": JWT_SECRET}):
        with Session(db_engine) as s:
            user = User(
                id=str(uuid.uuid4()),
                email="max@example.com",
                display_name="Max",
                password_hash=hash_password("hunter2hunter2"),
                is_active=True,
                role="admin",
            )
            s.add(user)
            s.commit()
            user_id = user.id

        from src.api.auth.jwt import issue_token

        token = issue_token(user_id)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def refund_seed(db_engine):
    """A self-contained March-2026 scenario covering the four canonical cases.

    - Apothekenrechnung -50 €  (gesundheit, expense)
    - Krankenkassen-Erstattung +30 € (gesundheit, is_refund=True)
    - Gehalt +3000 € (gehalt, normal income)
    - Steuerrückzahlung +800 € (erstattungen, NOT a refund — real income)
    - Umbuchung -500 € (internal_transfer=True — must not appear anywhere)
    """
    with Session(db_engine) as s:
        s.add(Category(id="gesundheit", name="Gesundheit", type=TypeEnum.VARIABEL))
        s.add(Category(id="gehalt", name="Gehalt", type=TypeEnum.FIX))
        s.add(Category(id="erstattungen", name="Erstattungen", type=TypeEnum.VARIABEL))
        s.flush()

        s.add(Budget(category_id="gesundheit", monthly_limit=Decimal("100.00"), currency="EUR"))

        for hash_char in ("a", "b", "c", "d", "e"):
            s.add(
                RawTransaction(
                    content_hash=hash_char * 64,
                    external_id=None,
                    raw_data={"stub": True},
                )
            )
        s.flush()

        rows = [
            NormalizedTransaction(
                id="apo",
                raw_content_hash="a" * 64,
                booking_date=date(2026, 3, 5),
                valuation_date=date(2026, 3, 5),
                amount=Decimal("-50.00"),
                currency="EUR",
                recipient="APOTHEKE",
                category_id="gesundheit",
                is_refund=False,
            ),
            NormalizedTransaction(
                id="kkn",
                raw_content_hash="b" * 64,
                booking_date=date(2026, 3, 20),
                valuation_date=date(2026, 3, 20),
                amount=Decimal("30.00"),
                currency="EUR",
                sender="TK Krankenkasse",
                category_id="gesundheit",
                is_refund=True,
            ),
            NormalizedTransaction(
                id="gehalt",
                raw_content_hash="c" * 64,
                booking_date=date(2026, 3, 1),
                valuation_date=date(2026, 3, 1),
                amount=Decimal("3000.00"),
                currency="EUR",
                sender="Arbeitgeber",
                category_id="gehalt",
                is_refund=False,
            ),
            NormalizedTransaction(
                id="steuer",
                raw_content_hash="d" * 64,
                booking_date=date(2026, 3, 10),
                valuation_date=date(2026, 3, 10),
                amount=Decimal("800.00"),
                currency="EUR",
                sender="Finanzamt",
                category_id="erstattungen",
                is_refund=False,
            ),
            NormalizedTransaction(
                id="umb",
                raw_content_hash="e" * 64,
                booking_date=date(2026, 3, 12),
                valuation_date=date(2026, 3, 12),
                amount=Decimal("-500.00"),
                currency="EUR",
                description="Umbuchung Tagesgeld",
                internal_transfer=True,
                is_refund=False,
            ),
        ]
        for row in rows:
            s.add(row)
        s.commit()


class TestMonthlySummaryRefund:
    def test_refund_excluded_from_income(self, api_client, refund_seed):
        resp = api_client.get(
            "/api/v1/aggregates/monthly-summary?year=2026&month=3", headers=AUTH
        )
        assert resp.status_code == 200
        data = resp.json()
        # Income = Gehalt 3000 + Steuer 800 = 3800. Krankenkasse darf NICHT
        # als Income zählen, obwohl Betrag positiv.
        assert Decimal(data["income"]) == Decimal("3800.00")

    def test_refund_reduces_expenses(self, api_client, refund_seed):
        resp = api_client.get(
            "/api/v1/aggregates/monthly-summary?year=2026&month=3", headers=AUTH
        )
        data = resp.json()
        # Expenses = -50 (Apotheke) + 30 (Refund) = -20. Refund senkt netto.
        assert Decimal(data["expenses"]) == Decimal("-20.00")

    def test_savings_rate_uses_net_expenses(self, api_client, refund_seed):
        resp = api_client.get(
            "/api/v1/aggregates/monthly-summary?year=2026&month=3", headers=AUTH
        )
        data = resp.json()
        # net = 3800 - 20 = 3780. savings_rate = 3780 / 3800 = 0.9947
        assert Decimal(data["net"]) == Decimal("3780.00")
        assert Decimal(data["savings_rate"]) == Decimal("0.9947")

    def test_internal_transfer_still_excluded(self, api_client, refund_seed):
        resp = api_client.get(
            "/api/v1/aggregates/monthly-summary?year=2026&month=3", headers=AUTH
        )
        data = resp.json()
        # transaction_count = 4 (Umbuchung gefiltert)
        assert data["transaction_count"] == 4


class TestCashflowOverTimeRefund:
    def test_refund_does_not_inflate_income_in_series(self, api_client, refund_seed):
        resp = api_client.get(
            "/api/v1/aggregates/cashflow-over-time?date_from=2026-03-01&date_to=2026-03-31",
            headers=AUTH,
        )
        assert resp.status_code == 200
        series = resp.json()["series"]
        assert len(series) == 1
        point = series[0]
        assert Decimal(point["income"]) == Decimal("3800.00")
        assert Decimal(point["expenses"]) == Decimal("-20.00")


class TestBudgetSpending:
    def test_returns_one_item_per_budget(self, api_client, refund_seed):
        resp = api_client.get(
            "/api/v1/aggregates/budget-spending?year=2026&month=3", headers=AUTH
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["category_id"] == "gesundheit"

    def test_spent_gross_only_negative_amounts(self, api_client, refund_seed):
        resp = api_client.get(
            "/api/v1/aggregates/budget-spending?year=2026&month=3", headers=AUTH
        )
        item = resp.json()["items"][0]
        assert Decimal(item["spent_gross"]) == Decimal("-50.00")

    def test_refunded_only_refund_flag(self, api_client, refund_seed):
        resp = api_client.get(
            "/api/v1/aggregates/budget-spending?year=2026&month=3", headers=AUTH
        )
        item = resp.json()["items"][0]
        assert Decimal(item["refunded"]) == Decimal("30.00")

    def test_spent_net_and_remaining(self, api_client, refund_seed):
        resp = api_client.get(
            "/api/v1/aggregates/budget-spending?year=2026&month=3", headers=AUTH
        )
        item = resp.json()["items"][0]
        # Limit 100, gross -50, refund +30 → net -20, remaining 80.
        assert Decimal(item["spent_net"]) == Decimal("-20.00")
        assert Decimal(item["remaining"]) == Decimal("80.00")
        assert item["transaction_count"] == 2

    def test_empty_month_returns_zero_spend(self, api_client, refund_seed):
        # Februar 2026 has zero transactions in the seed.
        resp = api_client.get(
            "/api/v1/aggregates/budget-spending?year=2026&month=2", headers=AUTH
        )
        item = resp.json()["items"][0]
        assert Decimal(item["spent_gross"]) == Decimal("0")
        assert Decimal(item["refunded"]) == Decimal("0")
        assert Decimal(item["remaining"]) == Decimal("100.00")
        assert item["transaction_count"] == 0


class TestTransactionRefundPatch:
    def test_patch_rejects_negative_refund(self, api_client, refund_seed):
        resp = api_client.patch(
            "/api/v1/transactions/apo",
            json={"is_refund": True},
            headers=AUTH,
        )
        assert resp.status_code == 422

    def test_patch_clears_is_refund(self, api_client, refund_seed):
        resp = api_client.patch(
            "/api/v1/transactions/kkn",
            json={"is_refund": False},
            headers=AUTH,
        )
        assert resp.status_code == 200
        assert resp.json()["is_refund"] is False

    def test_list_filter_is_refund(self, api_client, refund_seed):
        resp = api_client.get("/api/v1/transactions?is_refund=true", headers=AUTH)
        assert resp.status_code == 200
        ids = [t["id"] for t in resp.json()["items"]]
        assert ids == ["kkn"]

    def test_transaction_out_includes_is_refund(self, api_client, refund_seed):
        resp = api_client.get("/api/v1/transactions/kkn", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["is_refund"] is True

    def test_patch_sets_internal_transfer(self, api_client, refund_seed):
        resp = api_client.patch(
            "/api/v1/transactions/apo",
            json={"internal_transfer": True},
            headers=AUTH,
        )
        assert resp.status_code == 200
        assert resp.json()["internal_transfer"] is True


class TestRefundAudit:
    @pytest.fixture
    def audit_seed(self, db_engine):
        """A pure refund-audit fixture with the four canonical signals."""
        from src.core.db.models import Category, RawTransaction, TypeEnum

        with Session(db_engine) as s:
            s.add(Category(id="erstattungen", name="Erstattungen", type=TypeEnum.VARIABEL))
            s.add(Category(id="gesundheit", name="Gesundheit", type=TypeEnum.VARIABEL))
            s.add(Category(id="kleidung", name="Kleidung", type=TypeEnum.DISKRETIONAER))
            s.add(Category(id="restaurant-cafe", name="Restaurant", type=TypeEnum.DISKRETIONAER))
            s.flush()

            for h in ("aa", "bb", "cc", "dd", "ee"):
                s.add(RawTransaction(content_hash=h * 32, external_id=None, raw_data={}))
            s.flush()

            # Krankenkasse — should suggest gesundheit.
            s.add(NormalizedTransaction(
                id="aa" * 32, raw_content_hash="aa" * 32,
                booking_date=date(2026, 4, 5), valuation_date=date(2026, 4, 5),
                amount=Decimal("42.00"), currency="EUR",
                sender="Techniker Krankenkasse", recipient="John Doe",
                description="Erstattung Rezept",
                category_id="erstattungen", is_refund=False,
            ))
            # Amazon retoure — should suggest kleidung.
            s.add(NormalizedTransaction(
                id="bb" * 32, raw_content_hash="bb" * 32,
                booking_date=date(2026, 4, 10), valuation_date=date(2026, 4, 10),
                amount=Decimal("89.99"), currency="EUR",
                sender="Amazon EU", recipient="John Doe",
                description="Retoure",
                category_id="erstattungen", is_refund=False,
            ))
            # Steuerrückzahlung — real income, no suggestion.
            s.add(NormalizedTransaction(
                id="cc" * 32, raw_content_hash="cc" * 32,
                booking_date=date(2026, 3, 1), valuation_date=date(2026, 3, 1),
                amount=Decimal("847.50"), currency="EUR",
                sender="Finanzamt München", recipient="John Doe",
                description="Steuer 2025",
                category_id="erstattungen", is_refund=False,
            ))
            # Already flagged — must NOT appear.
            s.add(NormalizedTransaction(
                id="dd" * 32, raw_content_hash="dd" * 32,
                booking_date=date(2026, 4, 12), valuation_date=date(2026, 4, 12),
                amount=Decimal("30.00"), currency="EUR",
                sender="Splitwise", recipient="John Doe",
                description="Splitwise", category_id="restaurant-cafe",
                is_refund=True,
            ))
            # Negative amount in erstattungen (shouldn't happen, but guard
            # the filter): must NOT appear.
            s.add(NormalizedTransaction(
                id="ee" * 32, raw_content_hash="ee" * 32,
                booking_date=date(2026, 4, 14), valuation_date=date(2026, 4, 14),
                amount=Decimal("-5.00"), currency="EUR",
                sender="John Doe", recipient="Whatever",
                description="negative outlier", category_id="erstattungen",
                is_refund=False,
            ))
            s.commit()

    def test_returns_unflagged_positive_erstattungen(self, api_client, audit_seed):
        resp = api_client.get("/api/v1/aggregates/refund-audit", headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 3  # Krankenkasse, Amazon, Steuer

    def test_suggests_gesundheit_for_krankenkasse(self, api_client, audit_seed):
        resp = api_client.get("/api/v1/aggregates/refund-audit", headers=AUTH)
        cands = {c["id"]: c for c in resp.json()["candidates"]}
        kk = cands["aa" * 32]
        assert kk["suggested_category_id"] == "gesundheit"
        # Reason text varies by which pattern matches first ("TK-Erstattung"
        # vs generic "Krankenkassen-Erstattung"); both are correct.
        assert kk["suggested_reason"] is not None

    def test_suggests_kleidung_for_amazon(self, api_client, audit_seed):
        resp = api_client.get("/api/v1/aggregates/refund-audit", headers=AUTH)
        cands = {c["id"]: c for c in resp.json()["candidates"]}
        amzn = cands["bb" * 32]
        assert amzn["suggested_category_id"] == "kleidung"

    def test_finanzamt_returns_no_category_hint(self, api_client, audit_seed):
        resp = api_client.get("/api/v1/aggregates/refund-audit", headers=AUTH)
        cands = {c["id"]: c for c in resp.json()["candidates"]}
        steuer = cands["cc" * 32]
        # Finanzamt is real income — endpoint signals "keep as income" by
        # leaving suggested_category_id null.
        assert steuer["suggested_category_id"] is None
        assert "Einkommen" in (steuer["suggested_reason"] or "")

    def test_excludes_already_flagged(self, api_client, audit_seed):
        resp = api_client.get("/api/v1/aggregates/refund-audit", headers=AUTH)
        ids = {c["id"] for c in resp.json()["candidates"]}
        assert "dd" * 32 not in ids
        assert "ee" * 32 not in ids

    def test_marking_decided_persists_and_excludes(self, api_client, audit_seed):
        # Steuerrückzahlung — user clicks "Echtes Einkommen" → row stays in
        # erstattungen with is_refund=false, but the audit endpoint must not
        # re-surface it. The PATCH stamps refund_audit_decided_at server-side.
        steuer_id = "cc" * 32
        resp = api_client.patch(
            f"/api/v1/transactions/{steuer_id}",
            json={"refund_audit_decided": True},
            headers=AUTH,
        )
        assert resp.status_code == 200
        # Row state unchanged otherwise.
        assert resp.json()["is_refund"] is False
        assert resp.json()["category"]["id"] == "erstattungen"

        followup = api_client.get("/api/v1/aggregates/refund-audit", headers=AUTH)
        ids = {c["id"] for c in followup.json()["candidates"]}
        assert steuer_id not in ids

    def test_marking_as_refund_auto_stamps_decided(self, api_client, audit_seed):
        # Reclassify Krankenkasse as refund — the implicit audit decision
        # should be persisted so the row doesn't re-appear if (e.g.) the
        # user clears is_refund later for some reason.
        kk_id = "aa" * 32
        api_client.patch(
            f"/api/v1/transactions/{kk_id}",
            json={"category_id": "gesundheit", "is_refund": True},
            headers=AUTH,
        )
        # Manually clear is_refund — the audit must still treat it as decided.
        api_client.patch(
            f"/api/v1/transactions/{kk_id}",
            json={"is_refund": False, "category_id": "erstattungen"},
            headers=AUTH,
        )
        followup = api_client.get("/api/v1/aggregates/refund-audit", headers=AUTH)
        ids = {c["id"] for c in followup.json()["candidates"]}
        assert kk_id not in ids

    def test_clear_decision_resurfaces_candidate(self, api_client, audit_seed):
        steuer_id = "cc" * 32
        api_client.patch(
            f"/api/v1/transactions/{steuer_id}",
            json={"refund_audit_decided": True},
            headers=AUTH,
        )
        api_client.patch(
            f"/api/v1/transactions/{steuer_id}",
            json={"refund_audit_decided": False},
            headers=AUTH,
        )
        followup = api_client.get("/api/v1/aggregates/refund-audit", headers=AUTH)
        ids = {c["id"] for c in followup.json()["candidates"]}
        assert steuer_id in ids


class TestRefundAuditAutoApply:
    @pytest.fixture
    def mixed_seed(self, db_engine):
        """Seed covering all three confidence tiers:
          - Krankenkasse  → auto_apply=True (high)
          - Finanzamt     → auto_apply=True (high), real income
          - Splitwise     → auto_apply=False (medium)
          - "Anna Müller" → no heuristic match (no auto-apply)
        """
        from src.core.db.models import Category, RawTransaction, TypeEnum

        with Session(db_engine) as s:
            s.add(Category(id="erstattungen", name="Erstattungen", type=TypeEnum.VARIABEL))
            s.add(Category(id="gesundheit", name="Gesundheit", type=TypeEnum.VARIABEL))
            s.add(Category(id="restaurant-cafe", name="Restaurant", type=TypeEnum.DISKRETIONAER))
            s.flush()

            specs = [
                ("11" * 32, "Techniker Krankenkasse", "Erstattung Rezept", Decimal("42.00")),
                ("22" * 32, "Finanzamt München", "Steuer 2025", Decimal("847.50")),
                ("33" * 32, "Splitwise (Anna)", "Restaurant", Decimal("25.00")),
                ("44" * 32, "Anna Müller", "Privat", Decimal("18.00")),
            ]
            for h, _sender, _desc, _amt in specs:
                s.add(RawTransaction(content_hash=h, external_id=None, raw_data={}))
            s.flush()
            for h, sender, desc, amt in specs:
                s.add(NormalizedTransaction(
                    id=h,
                    raw_content_hash=h,
                    booking_date=date(2026, 4, 5),
                    valuation_date=date(2026, 4, 5),
                    amount=amt,
                    currency="EUR",
                    sender=sender,
                    recipient="John Doe",
                    description=desc,
                    category_id="erstattungen",
                    is_refund=False,
                ))
            s.commit()

    def test_auto_apply_endpoint_partitions_correctly(
        self, api_client, mixed_seed, user_auth
    ):
        resp = api_client.post(
            "/api/v1/aggregates/refund-audit/auto-apply", headers=user_auth
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["applied_refund"] == 1   # Krankenkasse
        assert body["applied_income"] == 1   # Finanzamt
        assert body["left_for_review"] == 2  # Splitwise + Anna Müller

    def test_auto_apply_rejects_service_token(self, api_client, mixed_seed):
        """The static API_TOKEN must NOT be allowed to flip historical
        categorizations — only an authenticated User can trigger the
        auto-apply. Guards against a stray Scheduler / MCP using its
        service token to silently mutate audit decisions.
        """
        resp = api_client.post(
            "/api/v1/aggregates/refund-audit/auto-apply", headers=AUTH
        )
        assert resp.status_code == 403
        assert "user authentication" in resp.json()["detail"]

    def test_auto_apply_rejects_anonymous(self, api_client, mixed_seed):
        resp = api_client.post("/api/v1/aggregates/refund-audit/auto-apply")
        assert resp.status_code == 401

    def test_auto_apply_writes_correct_state(
        self, api_client, mixed_seed, db_engine, user_auth
    ):
        api_client.post(
            "/api/v1/aggregates/refund-audit/auto-apply", headers=user_auth
        )
        with Session(db_engine) as s:
            kk = s.get(NormalizedTransaction, "11" * 32)
            steuer = s.get(NormalizedTransaction, "22" * 32)
            split = s.get(NormalizedTransaction, "33" * 32)
            anna = s.get(NormalizedTransaction, "44" * 32)

            # Krankenkasse: refund-flagged + retargeted + stamped.
            assert kk.is_refund is True
            assert kk.category_id == "gesundheit"
            assert kk.refund_audit_decided_at is not None

            # Finanzamt: real income, only stamped (no other change).
            assert steuer.is_refund is False
            assert steuer.category_id == "erstattungen"
            assert steuer.refund_audit_decided_at is not None

            # Splitwise & Anna: untouched, surfaced for manual review.
            assert split.is_refund is False
            assert split.category_id == "erstattungen"
            assert split.refund_audit_decided_at is None
            assert anna.refund_audit_decided_at is None

    def test_audit_endpoint_only_shows_review_candidates(
        self, api_client, mixed_seed, user_auth
    ):
        api_client.post(
            "/api/v1/aggregates/refund-audit/auto-apply", headers=user_auth
        )
        resp = api_client.get("/api/v1/aggregates/refund-audit", headers=AUTH)
        ids = {c["id"] for c in resp.json()["candidates"]}
        # Auto-applied rows are gone; only the ambiguous ones remain.
        assert ids == {"33" * 32, "44" * 32}

    def test_idempotent_second_run_changes_nothing(
        self, api_client, mixed_seed, user_auth
    ):
        first = api_client.post(
            "/api/v1/aggregates/refund-audit/auto-apply", headers=user_auth
        ).json()
        second = api_client.post(
            "/api/v1/aggregates/refund-audit/auto-apply", headers=user_auth
        ).json()
        assert first["applied_refund"] + first["applied_income"] == 2
        assert second["applied_refund"] == 0
        assert second["applied_income"] == 0
        assert second["left_for_review"] == 2
