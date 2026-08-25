"""Audit-history regressions for the remitter re-ingest maintenance script."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from scripts.reingest_comdirect_remitter import (
    _carry_fields_and_refresh_accounting,
    _collect_carry_fields,
    _reingest_atomically,
)
from src.core.db.models import (
    Category,
    Base,
    DataSource,
    MailEvidence,
    NormalizedTransaction,
    RawTransaction,
    ReviewedSuggestion,
    SubscriptionRecord,
    SyncRun,
    Tag,
    TransactionEvidenceLink,
    TransactionLink,
    TransactionTag,
    TypeEnum,
    User,
    ValueAssessment,
)
from src.normalization.canonicalize import canonicalize, content_hash
from src.normalization.pipeline import NormalizationPipeline


@pytest.fixture
def graph_engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _tx(tx_id: str, raw_hash: str, *, active: bool = True) -> NormalizedTransaction:
    return NormalizedTransaction(
        id=tx_id,
        raw_content_hash=raw_hash,
        booking_date=date(2026, 1, 1),
        valuation_date=date(2026, 1, 1),
        amount=Decimal("1.00"),
        currency="EUR",
        is_active=active,
        normalization_status="active" if active else "superseded",
        is_recurring=False,
        is_outlier=False,
        internal_transfer=False,
        accounting_version=1,
    )


def _seed_reference_pair(session: Session) -> tuple[str, str]:
    old_hash = "a" * 64
    successor_hash = "b" * 64
    session.add_all(
        [
            RawTransaction(content_hash=old_hash, raw_data={"stub": True}),
            RawTransaction(content_hash=successor_hash, raw_data={"stub": True}),
            _tx("predecessor", old_hash),
            _tx("successor", successor_hash),
        ]
    )
    return old_hash, successor_hash


def _dummy_user(user_id: str, suffix: str) -> User:
    return User(
        id=user_id,
        email=f"john-{suffix}@example.invalid",
        display_name="John Doe",
        password_hash="dummy-hash",
    )


def test_carry_preserves_predecessor_and_link_history_and_refreshes_successor(
    db_engine,
):
    old_hash = "4" * 64
    successor_hash = "5" * 64
    child_hash = "6" * 64
    decided_at = datetime(2026, 1, 2, tzinfo=timezone.utc)

    with Session(db_engine) as session:
        session.add(Category(id="groceries", name="Groceries", type=TypeEnum.VARIABEL))
        session.add_all(
            [
                RawTransaction(content_hash=old_hash, raw_data={"stub": True}),
                RawTransaction(content_hash=successor_hash, raw_data={"stub": True}),
                RawTransaction(content_hash=child_hash, raw_data={"stub": True}),
            ]
        )
        session.flush()
        predecessor = _tx("predecessor", old_hash)
        predecessor.category_id = "groceries"
        predecessor.is_refund = True
        predecessor.refund_verification_status = "user_verified"
        predecessor.refund_audit_decided_at = decided_at
        session.add_all([predecessor, _tx("linked-child", child_hash)])
        session.flush()
        session.add(
            TransactionLink(
                id="retained-link",
                parent_transaction_id="predecessor",
                child_transaction_id="linked-child",
                link_type="manual",
            )
        )
        session.commit()

        carry = _collect_carry_fields(session, [(old_hash, successor_hash)])
        predecessor.is_active = False
        predecessor.normalization_status = "superseded"
        predecessor.superseded_by_id = successor_hash
        session.add(_tx("successor", successor_hash))
        session.commit()

    with Session(db_engine) as session:
        assert _carry_fields_and_refresh_accounting(session, carry) == 1

    with Session(db_engine) as session:
        predecessor = session.get(NormalizedTransaction, "predecessor")
        successor = session.get(NormalizedTransaction, "successor")
        link = session.get(TransactionLink, "retained-link")
        assert predecessor is not None and predecessor.is_active is False
        assert predecessor.category_id == "groceries"
        assert link is not None
        assert link.parent_transaction_id == "predecessor"
        assert successor.category_id == "groceries"
        assert successor.is_refund is True
        assert successor.refund_verification_status == "user_verified"
        assert successor.refund_audit_decided_at == decided_at
        assert successor.accounting_class == "verified_refund_reimbursement"
        assert successor.accounting_confidence == Decimal("1.000")
        assert successor.accounting_version == 2


def test_carry_migrates_complete_reference_graph_without_duplicates(graph_engine):
    old_hash = "7" * 64
    successor_hash = "8" * 64

    with Session(graph_engine) as session:
        session.add_all(
            [
                RawTransaction(content_hash=old_hash, raw_data={"stub": True}),
                RawTransaction(content_hash=successor_hash, raw_data={"stub": True}),
                _tx("predecessor", old_hash),
                _tx("successor", successor_hash),
                Tag(id="reviewed", name="Reviewed"),
                MailEvidence(
                    id="dummy-evidence",
                    evidence_type="invoice",
                    order_ref_hash="0" * 64,
                    confidence=Decimal("1.000"),
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                TransactionTag(transaction_id="predecessor", tag_id="reviewed"),
                ReviewedSuggestion(
                    transaction_id="predecessor", reason="user rejected suggestion"
                ),
                TransactionEvidenceLink(
                    id="old-evidence-link",
                    transaction_id="predecessor",
                    evidence_id="dummy-evidence",
                    match_type="amount_date",
                    confidence=Decimal("1.000"),
                    match_reason="dummy exact match",
                ),
                SubscriptionRecord(
                    id="dummy-subscription",
                    label="Dummy subscription",
                    status="review",
                    confidence=Decimal("0.500"),
                    evidence_source="manual",
                    transaction_id="predecessor",
                ),
                ValueAssessment(
                    id="dummy-assessment",
                    transaction_id="predecessor",
                    value_class="uncertain",
                    confidence=Decimal("0.500"),
                ),
            ]
        )
        session.commit()

        carry = _collect_carry_fields(session, [(old_hash, successor_hash)])
        predecessor = session.get(NormalizedTransaction, "predecessor")
        predecessor.is_active = False
        predecessor.normalization_status = "superseded"
        assert _carry_fields_and_refresh_accounting(session, carry) == 1
        assert _carry_fields_and_refresh_accounting(session, carry) == 1

    with Session(graph_engine) as session:
        assert session.get(TransactionTag, ("predecessor", "reviewed")) is not None
        assert session.get(TransactionTag, ("successor", "reviewed")) is not None
        assert session.get(ReviewedSuggestion, "predecessor") is not None
        assert session.get(ReviewedSuggestion, "successor") is not None
        successor_links = session.query(TransactionEvidenceLink).filter_by(
            transaction_id="successor",
            evidence_id="dummy-evidence",
            match_type="amount_date",
        )
        assert successor_links.count() == 1
        assert session.get(TransactionEvidenceLink, "old-evidence-link") is not None
        assert session.get(SubscriptionRecord, "dummy-subscription").transaction_id == (
            "successor"
        )
        assert session.get(ValueAssessment, "dummy-assessment").transaction_id == (
            "successor"
        )


def test_carry_reuses_equivalent_reviewed_suggestion_idempotently(graph_engine):
    decided_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
    with Session(graph_engine) as session:
        old_hash, successor_hash = _seed_reference_pair(session)
        session.add_all(
            [
                ReviewedSuggestion(
                    transaction_id="predecessor",
                    reason="user-rejected",
                    decided_at=decided_at,
                ),
                ReviewedSuggestion(
                    transaction_id="successor",
                    reason="user-rejected",
                    decided_at=decided_at,
                ),
            ]
        )
        session.commit()
        carry = _collect_carry_fields(session, [(old_hash, successor_hash)])

        assert _carry_fields_and_refresh_accounting(session, carry) == 1
        assert _carry_fields_and_refresh_accounting(session, carry) == 1
        assert session.query(ReviewedSuggestion).count() == 2


@pytest.mark.parametrize(
    "successor_reason,successor_decided_at",
    [
        ("different-decision", datetime(2026, 1, 2, tzinfo=timezone.utc)),
        ("user-rejected", datetime(2026, 1, 3, tzinfo=timezone.utc)),
    ],
)
def test_carry_rejects_conflicting_reviewed_suggestion_fields(
    graph_engine, successor_reason, successor_decided_at
):
    decided_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
    with Session(graph_engine) as session:
        old_hash, successor_hash = _seed_reference_pair(session)
        session.add_all(
            [
                ReviewedSuggestion(
                    transaction_id="predecessor",
                    reason="user-rejected",
                    decided_at=decided_at,
                ),
                ReviewedSuggestion(
                    transaction_id="successor",
                    reason=successor_reason,
                    decided_at=successor_decided_at,
                ),
            ]
        )
        session.commit()
        carry = _collect_carry_fields(session, [(old_hash, successor_hash)])

        with pytest.raises(RuntimeError, match="conflicting reviewed suggestions"):
            _carry_fields_and_refresh_accounting(session, carry)


def test_carry_reuses_equivalent_mail_evidence_link_idempotently(graph_engine):
    linked_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
    with Session(graph_engine) as session:
        old_hash, successor_hash = _seed_reference_pair(session)
        session.add(
            MailEvidence(
                id="dummy-evidence",
                evidence_type="invoice",
                order_ref_hash="0" * 64,
                confidence=Decimal("1.000"),
            )
        )
        session.flush()
        for link_id, transaction_id in (
            ("predecessor-link", "predecessor"),
            ("successor-link", "successor"),
        ):
            session.add(
                TransactionEvidenceLink(
                    id=link_id,
                    transaction_id=transaction_id,
                    evidence_id="dummy-evidence",
                    match_type="amount_date",
                    confidence=Decimal("0.900"),
                    match_reason="dummy compatible match",
                    created_at=linked_at,
                )
            )
        session.commit()
        carry = _collect_carry_fields(session, [(old_hash, successor_hash)])

        assert _carry_fields_and_refresh_accounting(session, carry) == 1
        assert _carry_fields_and_refresh_accounting(session, carry) == 1
        assert (
            session.query(TransactionEvidenceLink)
            .filter_by(transaction_id="successor")
            .count()
            == 1
        )


@pytest.mark.parametrize(
    "successor_confidence,successor_reason,successor_created_at",
    [
        (
            Decimal("0.800"),
            "dummy compatible match",
            datetime(2026, 1, 2, tzinfo=timezone.utc),
        ),
        (
            Decimal("0.900"),
            "dummy conflicting match",
            datetime(2026, 1, 2, tzinfo=timezone.utc),
        ),
        (
            Decimal("0.900"),
            "dummy compatible match",
            datetime(2026, 1, 3, tzinfo=timezone.utc),
        ),
    ],
)
def test_carry_rejects_conflicting_mail_evidence_link_fields(
    graph_engine,
    successor_confidence,
    successor_reason,
    successor_created_at,
):
    linked_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
    with Session(graph_engine) as session:
        old_hash, successor_hash = _seed_reference_pair(session)
        session.add(
            MailEvidence(
                id="dummy-evidence",
                evidence_type="invoice",
                order_ref_hash="0" * 64,
                confidence=Decimal("1.000"),
            )
        )
        session.flush()
        session.add_all(
            [
                TransactionEvidenceLink(
                    id="predecessor-link",
                    transaction_id="predecessor",
                    evidence_id="dummy-evidence",
                    match_type="amount_date",
                    confidence=Decimal("0.900"),
                    match_reason="dummy compatible match",
                    created_at=linked_at,
                ),
                TransactionEvidenceLink(
                    id="successor-link",
                    transaction_id="successor",
                    evidence_id="dummy-evidence",
                    match_type="amount_date",
                    confidence=successor_confidence,
                    match_reason=successor_reason,
                    created_at=successor_created_at,
                ),
            ]
        )
        session.commit()
        carry = _collect_carry_fields(session, [(old_hash, successor_hash)])

        with pytest.raises(RuntimeError, match="conflicting mail-evidence links"):
            _carry_fields_and_refresh_accounting(session, carry)


def test_carry_reuses_equivalent_same_owner_subscription_idempotently(graph_engine):
    owner_id = "00000000-0000-0000-0000-000000000001"
    created_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
    common = {
        "label": "Dummy subscription",
        "status": "review",
        "confidence": Decimal("0.500"),
        "evidence_source": "manual",
        "owner_user_id": owner_id,
        "amount_scenarios": ["0.00"],
        "next_review_date": date(2026, 2, 1),
        "created_at": created_at,
    }
    with Session(graph_engine) as session:
        old_hash, successor_hash = _seed_reference_pair(session)
        session.add(_dummy_user(owner_id, "owner"))
        session.add_all(
            [
                SubscriptionRecord(
                    id="predecessor-subscription",
                    transaction_id="predecessor",
                    **common,
                ),
                SubscriptionRecord(
                    id="successor-subscription",
                    transaction_id="successor",
                    **common,
                ),
            ]
        )
        session.commit()
        carry = _collect_carry_fields(session, [(old_hash, successor_hash)])

        assert _carry_fields_and_refresh_accounting(session, carry) == 1
        assert _carry_fields_and_refresh_accounting(session, carry) == 1
        assert session.get(SubscriptionRecord, "predecessor-subscription").transaction_id == (
            "predecessor"
        )
        assert session.get(SubscriptionRecord, "successor-subscription").transaction_id == (
            "successor"
        )


@pytest.mark.parametrize(
    "successor_owner_id,successor_status,error",
    [
        (
            "00000000-0000-0000-0000-000000000002",
            "review",
            "mixed-owner subscription evidence",
        ),
        (
            "00000000-0000-0000-0000-000000000001",
            "booked_payment",
            "conflicting subscription evidence",
        ),
    ],
)
def test_carry_rejects_incompatible_successor_subscription(
    graph_engine, successor_owner_id, successor_status, error
):
    owner_id = "00000000-0000-0000-0000-000000000001"
    created_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
    with Session(graph_engine) as session:
        old_hash, successor_hash = _seed_reference_pair(session)
        session.add_all(
            [
                _dummy_user(owner_id, "owner-a"),
                _dummy_user(
                    "00000000-0000-0000-0000-000000000002", "owner-b"
                ),
            ]
        )
        for record_id, transaction_id, record_owner, status in (
            ("predecessor-subscription", "predecessor", owner_id, "review"),
            (
                "successor-subscription",
                "successor",
                successor_owner_id,
                successor_status,
            ),
        ):
            session.add(
                SubscriptionRecord(
                    id=record_id,
                    label="Dummy subscription",
                    status=status,
                    confidence=Decimal("0.500"),
                    evidence_source="manual",
                    owner_user_id=record_owner,
                    transaction_id=transaction_id,
                    amount_scenarios=["0.00"],
                    next_review_date=date(2026, 2, 1),
                    created_at=created_at,
                )
            )
        session.commit()
        carry = _collect_carry_fields(session, [(old_hash, successor_hash)])

        with pytest.raises(RuntimeError, match=error):
            _carry_fields_and_refresh_accounting(session, carry)


def test_carry_rejects_cross_user_evidence_graph_and_rolls_back_all_writes(
    graph_engine,
):
    owner_a = "00000000-0000-0000-0000-000000000001"
    owner_b = "00000000-0000-0000-0000-000000000002"
    linked_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
    with Session(graph_engine) as session:
        old_hash, successor_hash = _seed_reference_pair(session)
        predecessor = session.get(NormalizedTransaction, "predecessor")
        predecessor.is_refund = True
        session.add_all(
            [
                _dummy_user(owner_a, "owner-a"),
                _dummy_user(owner_b, "owner-b"),
                Tag(id="reviewed", name="Reviewed"),
                MailEvidence(
                    id="dummy-evidence",
                    evidence_type="invoice",
                    order_ref_hash="0" * 64,
                    confidence=Decimal("1.000"),
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                TransactionTag(transaction_id="predecessor", tag_id="reviewed"),
                ReviewedSuggestion(
                    transaction_id="predecessor",
                    reason="user-rejected",
                    decided_at=linked_at,
                ),
                TransactionEvidenceLink(
                    id="predecessor-link",
                    transaction_id="predecessor",
                    evidence_id="dummy-evidence",
                    match_type="amount_date",
                    confidence=Decimal("0.900"),
                    match_reason="dummy compatible match",
                    created_at=linked_at,
                ),
                SubscriptionRecord(
                    id="predecessor-subscription",
                    label="Dummy subscription",
                    status="review",
                    confidence=Decimal("0.500"),
                    evidence_source="manual",
                    owner_user_id=owner_a,
                    transaction_id="predecessor",
                ),
                ValueAssessment(
                    id="successor-assessment",
                    transaction_id="successor",
                    owner_user_id=owner_b,
                    value_class="uncertain",
                    confidence=Decimal("0.500"),
                ),
            ]
        )
        session.commit()
        carry = _collect_carry_fields(session, [(old_hash, successor_hash)])

    with Session(graph_engine) as session:
        with pytest.raises(RuntimeError, match="mixed-owner subscription evidence"):
            with session.begin():
                _carry_fields_and_refresh_accounting(session, carry, commit=False)

    with Session(graph_engine) as session:
        successor = session.get(NormalizedTransaction, "successor")
        assert successor.is_refund is False
        assert session.get(TransactionTag, ("successor", "reviewed")) is None
        assert session.get(ReviewedSuggestion, "successor") is None
        assert (
            session.query(TransactionEvidenceLink)
            .filter_by(transaction_id="successor")
            .count()
            == 0
        )
        assert session.get(SubscriptionRecord, "predecessor-subscription").transaction_id == (
            "predecessor"
        )
        assert session.get(ValueAssessment, "successor-assessment").transaction_id == (
            "successor"
        )


@pytest.mark.parametrize("external_id", ["DUMMY-REFERENCE-1", None])
def test_reingest_failure_rolls_back_every_stage_and_retry_is_idempotent(
    db_engine, external_id,
):
    old_data = {
        "reference": "DUMMY-REFERENCE-1",
        "booking_date": "2026-01-01",
        "value_date": "2026-01-01",
        "amount": "25.00",
        "currency": "EUR",
        "debtor_name": "",
        "debtor_iban": "",
        "description": "Dummy reimbursement",
    }
    new_data = {
        **old_data,
        "debtor_name": "John Doe",
        "debtor_iban": "DE00000000000000000000",
    }
    old_hash = content_hash(canonicalize(old_data), source="comdirect")
    new_hash = content_hash(canonicalize(new_data), source="comdirect")
    assert old_hash != new_hash

    with Session(db_engine) as session:
        session.add(Category(id="groceries", name="Groceries", type=TypeEnum.VARIABEL))
        session.add(
            RawTransaction(
                content_hash=old_hash,
                source=DataSource.COMDIRECT,
                external_id=external_id,
                raw_data=old_data,
            )
        )
        session.flush()
        predecessor = _tx(old_hash, old_hash)
        predecessor.category_id = "groceries"
        predecessor.is_refund = True
        predecessor.refund_verification_status = "user_verified"
        predecessor.refund_audit_decided_at = datetime(
            2026, 1, 2, tzinfo=timezone.utc
        )
        session.add(predecessor)
        session.commit()

    reingest = [
        {
            "content_hash": new_hash,
            "raw_data": new_data,
            "source": DataSource.COMDIRECT,
            "external_id": external_id,
            "batch_id": None,
        }
    ]
    transitions = [(old_hash, new_hash)]
    pipeline = NormalizationPipeline(
        db_engine.url.render_as_string(hide_password=False), own_ibans=[]
    )

    def fail_after_normalization(stage: str) -> None:
        if stage == "after_normalization":
            raise RuntimeError("injected remitter re-ingest failure")

    try:
        with pytest.raises(RuntimeError, match="injected remitter re-ingest failure"):
            _reingest_atomically(
                pipeline,
                reingest,
                transitions,
                failure_hook=fail_after_normalization,
            )

        with Session(db_engine) as session:
            assert session.get(RawTransaction, old_hash).superseded_by is None
            assert session.get(RawTransaction, new_hash) is None
            assert session.get(NormalizedTransaction, old_hash).is_active is True
            assert session.get(NormalizedTransaction, new_hash) is None
            assert session.query(SyncRun).count() == 0

        inserted, _run_id, carried = _reingest_atomically(
            pipeline, reingest, transitions
        )
        assert (inserted, carried) == (1, 1)
        with Session(db_engine) as session:
            assert session.get(RawTransaction, old_hash).superseded_by == new_hash
            assert session.get(RawTransaction, new_hash).version == 2
            assert session.get(NormalizedTransaction, old_hash).is_active is False
            successor = session.get(NormalizedTransaction, new_hash)
            assert successor.is_active is True
            assert successor.category_id == "groceries"
            assert successor.is_refund is True
            assert successor.refund_verification_status == "user_verified"
            assert successor.accounting_class == "verified_refund_reimbursement"
            assert session.query(SyncRun).count() == 1

        retry_inserted, _retry_run_id, retry_carried = _reingest_atomically(
            pipeline, reingest, transitions
        )
        assert (retry_inserted, retry_carried) == (0, 0)
        with Session(db_engine) as session:
            assert session.query(RawTransaction).count() == 2
            assert session.query(NormalizedTransaction).count() == 2
            assert session.get(NormalizedTransaction, new_hash).is_refund is True
    finally:
        pipeline.engine.dispose()
