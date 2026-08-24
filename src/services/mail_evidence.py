"""Mail evidence extraction and transaction matching.

This module is deliberately provider-neutral. Real Gmail access can feed the
same ``MailMessageImport`` shape later, but only sanitized evidence rows are
persisted. Raw mail bodies stay at the API edge.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from src.core.db.models import (
    MailEvidence,
    NormalizedTransaction,
    TransactionEvidenceLink,
)

_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_DE_IBAN_RE = re.compile(r"\bDE\d{20}\b")
_GENERIC_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")
_LONG_DIGITS_RE = re.compile(r"\b\d{7,}\b")
_ORDER_LIKE_RE = re.compile(
    r"(?i)\b(order|bestell(?:ung|nummer)?|invoice|rechnung(?:snummer)?|ref)"
    r"[\s:#-]*[A-Z0-9][A-Z0-9._/-]{3,}\b"
)
_SPACES_RE = re.compile(r"\s+")


def hash_reference(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()


def normalize_merchant(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.lower()
    cleaned = cleaned.replace("paypal *", " ")
    cleaned = re.sub(r"\b(gmbh|ag|inc|ltd|co\.?|kg|se|eu|payments?)\b", " ", cleaned)
    cleaned = re.sub(r"[^a-z0-9äöüß]+", " ", cleaned)
    cleaned = _SPACES_RE.sub(" ", cleaned).strip()
    return cleaned or None


def redact_free_text(value: str | None, *, limit: int = 500) -> str | None:
    if not value:
        return None
    out = _EMAIL_RE.sub("[email]", value)
    out = _DE_IBAN_RE.sub("DE[iban]", out)
    out = _GENERIC_IBAN_RE.sub("[iban]", out)
    out = _ORDER_LIKE_RE.sub(lambda m: f"{m.group(1)} [ref]", out)
    out = _LONG_DIGITS_RE.sub("[number]", out)
    out = _SPACES_RE.sub(" ", out).strip()
    return out[:limit] if out else None


def _first_match(patterns: tuple[str, ...], text: str) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, re.I | re.M)
        if match:
            return match.group(1).strip()
    return None


def _parse_decimal(value: str | None) -> Decimal | None:
    if not value:
        return None
    cleaned = value.strip()
    if "," in cleaned and "." in cleaned:
        normalized = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        normalized = cleaned.replace(",", ".")
    else:
        normalized = cleaned
    try:
        return Decimal(normalized).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            from datetime import datetime

            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _detect_evidence_type(text: str) -> str:
    lowered = text.lower()
    refund_patterns = (
        r"\brefund(?:ed)?\b",
        r"\brückerstattung\b",
        r"\berstattung\b",
        r"\berstattet\b",
        r"\bgutschrift\b",
    )
    if any(re.search(pattern, lowered) for pattern in refund_patterns):
        return "refund"
    if any(term in lowered for term in ("subscription", "abo", "verlängerung")):
        return "subscription"
    if any(term in lowered for term in ("receipt", "quittung", "beleg", "kassenbon")):
        return "receipt"
    if any(term in lowered for term in ("invoice", "rechnung")):
        return "invoice"
    return "order"


def _detect_payment_method(text: str) -> str | None:
    lowered = text.lower()
    if "paypal" in lowered:
        return "paypal"
    if any(term in lowered for term in ("visa", "mastercard", "kreditkarte", "credit card")):
        return "credit_card"
    if any(term in lowered for term in ("sepa", "lastschrift", "direct debit")):
        return "direct_debit"
    if any(term in lowered for term in ("kauf auf rechnung", "zahlungsart: invoice", "payment method: invoice")):
        return "invoice"
    return None


def _extract_line_items(text: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith(("-", "*")) or "|" not in line:
            continue
        parts = [p.strip() for p in line[1:].split("|")]
        if not parts or not parts[0]:
            continue
        item: dict[str, str] = {"label": redact_free_text(parts[0], limit=120) or "Artikel"}
        if len(parts) > 1:
            amount = _parse_decimal(parts[1])
            if amount is not None:
                item["amount"] = str(amount)
        if len(parts) > 2 and parts[2]:
            item["category_hint"] = redact_free_text(parts[2], limit=80) or ""
        items.append(item)
    return items[:30]


def extract_evidence_from_message(message: dict[str, Any]) -> dict[str, Any]:
    """Extract a sanitized evidence draft from one mail-like message.

    The parser intentionally supports a simple deterministic mock format first:
    ``Merchant:``, ``Date:``, ``Total:``, ``Order:``, ``Payment:`` and optional
    item lines in ``- label | amount | category_hint`` form. Real provider
    extraction can produce the same draft shape later.
    """

    subject = str(message.get("subject") or "")
    body = str(message.get("body_text") or "")
    text = f"{subject}\n{body}"
    merchant = _first_match(
        (
            r"^\s*(?:merchant|händler|shop|vendor)\s*:\s*(.+)$",
            r"^\s*(?:from|von)\s*:\s*(.+)$",
        ),
        text,
    )
    if merchant is None:
        merchant = str(message.get("sender") or "").split("@", maxsplit=1)[0]
    merchant = redact_free_text(merchant, limit=200)

    amount_raw = _first_match(
        (
            r"^\s*(?:total|betrag|summe|amount)\s*:\s*(?:EUR|€)?\s*(-?\d+[\d.,]*)",
            r"^\s*(?:total|betrag|summe|amount)\s*:\s*(-?\d+[\d.,]*)\s*(?:EUR|€)",
            r"^\s*(?:gesamtbetrag|rechnungsbetrag|offener betrag)(?:\s+inkl\.\s+mwst\.)?\s*:\s*(-?\d+[\d.,]*)\s*(?:EUR|€)",
            r"\b(?:betrag|summe|gesamtbetrag|rechnungsbetrag)\s+von\s*(-?\d+[\d.,]*)\s*(?:EUR|€)",
            r"\b(?:offener betrag|gesamtbetrag|rechnungsbetrag)(?:\s+inkl\.\s+mwst\.)?\s*:\s*(-?\d+[\d.,]*)\s*(?:EUR|€)",
        ),
        text,
    )
    currency = (_first_match((r"\b(EUR|USD|GBP|CHF)\b",), text) or "EUR").upper()
    document_date = _parse_date(
        _first_match((r"^\s*(?:date|datum|order date|bestelldatum)\s*:\s*([0-9./-]+)",), text)
    )
    order_ref = _first_match(
        (
            r"^\s*(?:order|bestellnummer|bestellung|invoice|rechnung)\s*:\s*(.+)$",
            r"\b(?:order|bestellnummer|bestellung|invoice|rechnung)[\s:#-]+([A-Z0-9._/-]{4,})\b",
        ),
        text,
    )
    payment_method = _detect_payment_method(text)
    confidence = Decimal("0.10")
    total_amount = _parse_decimal(amount_raw)
    if merchant:
        confidence += Decimal("0.20")
    if total_amount is not None:
        confidence += Decimal("0.25")
    if document_date is not None:
        confidence += Decimal("0.15")
    if order_ref:
        confidence += Decimal("0.15")
    if payment_method:
        confidence += Decimal("0.10")
    line_items = _extract_line_items(body)
    if line_items:
        confidence += Decimal("0.05")

    source_message_id = str(message.get("source_message_id") or subject or body)
    order_ref_hash = hash_reference(order_ref) if order_ref else None
    fallback_hash = hash_reference(source_message_id)
    identity = "|".join(
        [
            str(message.get("source") or "gmail"),
            order_ref_hash or fallback_hash,
            _detect_evidence_type(text),
            str(total_amount or ""),
            str(document_date or ""),
        ]
    )

    return {
        "id": f"mail_{hash_reference(identity)[:24]}",
        "source": str(message.get("source") or "gmail"),
        "evidence_type": _detect_evidence_type(text),
        "merchant_name": merchant,
        "merchant_key": normalize_merchant(merchant),
        "document_date": document_date or message.get("received_at"),
        "total_amount": total_amount.copy_abs() if total_amount is not None else None,
        "currency": currency,
        "payment_method": payment_method,
        "payment_hint": redact_free_text(
            _first_match((r"^\s*(?:payment|zahlung|zahlungsart)\s*:\s*(.+)$",), text),
            limit=200,
        ),
        "order_ref_hash": order_ref_hash,
        "subject_hint": redact_free_text(subject, limit=200),
        "redacted_snippet": redact_free_text(body, limit=500),
        "line_items": line_items,
        "confidence": min(confidence, Decimal("0.999")),
    }


def upsert_mail_evidence(session: Session, draft: dict[str, Any]) -> MailEvidence:
    evidence = session.get(MailEvidence, draft["id"])
    if evidence is None and draft.get("order_ref_hash"):
        evidence = session.execute(
            select(MailEvidence).where(
                MailEvidence.source == draft["source"],
                MailEvidence.order_ref_hash == draft["order_ref_hash"],
                MailEvidence.evidence_type == draft["evidence_type"],
            )
        ).scalar_one_or_none()

    if evidence is None:
        evidence = MailEvidence(**draft)
        session.add(evidence)
    else:
        for key, value in draft.items():
            if key != "id":
                setattr(evidence, key, value)
    session.flush()
    return evidence


def _amount_cents(value: Decimal) -> int:
    return int((value * 100).quantize(Decimal("1")))


def _haystack(tx: NormalizedTransaction) -> str:
    return normalize_merchant(
        " ".join(part for part in (tx.sender, tx.recipient, tx.description) if part)
    ) or ""


def _score_candidate(evidence: MailEvidence, tx: NormalizedTransaction) -> tuple[Decimal, str]:
    if evidence.total_amount is None:
        return Decimal("0"), "no evidence amount"
    if _amount_cents(evidence.total_amount) != abs(_amount_cents(tx.amount)):
        return Decimal("0"), "amount mismatch"

    expected_credit = evidence.evidence_type == "refund"
    if expected_credit and tx.amount <= 0:
        return Decimal("0"), "refund evidence expects a credit"
    if not expected_credit and tx.amount >= 0:
        return Decimal("0"), "purchase evidence expects a debit"

    score = Decimal("0.55")
    reasons = ["amount"]

    if evidence.document_date:
        delta = abs((tx.booking_date - evidence.document_date).days)
        if delta <= 1:
            score += Decimal("0.15")
            reasons.append("date<=1d")
        elif delta <= 5:
            score += Decimal("0.08")
            reasons.append("date<=5d")

    tx_text = _haystack(tx)
    if evidence.merchant_key:
        merchant_tokens = [t for t in evidence.merchant_key.split() if len(t) >= 4]
        if merchant_tokens and any(token in tx_text for token in merchant_tokens):
            score += Decimal("0.20")
            reasons.append("merchant")

    if evidence.payment_method and evidence.payment_method in tx_text:
        score += Decimal("0.10")
        reasons.append("payment_method")
    elif evidence.payment_method == "credit_card" and any(
        token in tx_text for token in ("visa", "mastercard", "santander")
    ):
        score += Decimal("0.10")
        reasons.append("payment_method")

    return min(score, Decimal("0.999")), "+".join(reasons)


def match_evidence_to_transactions(
    session: Session,
    evidence: MailEvidence,
    *,
    date_window_days: int = 7,
    min_confidence: Decimal = Decimal("0.65"),
) -> list[TransactionEvidenceLink]:
    if evidence.total_amount is None or evidence.document_date is None:
        return []

    start = evidence.document_date - timedelta(days=date_window_days)
    end = evidence.document_date + timedelta(days=date_window_days)
    candidates = session.execute(
        select(NormalizedTransaction)
        .where(
            and_(
                NormalizedTransaction.booking_date >= start,
                NormalizedTransaction.booking_date <= end,
                NormalizedTransaction.is_active.is_(True),
                NormalizedTransaction.internal_transfer.is_(False),
            )
        )
        .order_by(NormalizedTransaction.booking_date)
    ).scalars()

    links: list[TransactionEvidenceLink] = []
    for tx in candidates:
        score, reason = _score_candidate(evidence, tx)
        if score < min_confidence:
            continue
        match_type = f"{evidence.evidence_type}_match"
        link_id = f"evlink_{hash_reference(f'{tx.id}|{evidence.id}|{match_type}')[:24]}"
        link = session.get(TransactionEvidenceLink, link_id)
        if link is None:
            link = TransactionEvidenceLink(
                id=link_id,
                transaction_id=tx.id,
                evidence_id=evidence.id,
                match_type=match_type,
                confidence=score,
                match_reason=reason,
            )
            session.add(link)
        else:
            link.confidence = score
            link.match_reason = reason
        links.append(link)
    session.flush()
    return links


def import_mail_message(session: Session, message: dict[str, Any]) -> tuple[MailEvidence, list[TransactionEvidenceLink]]:
    draft = extract_evidence_from_message(message)
    evidence = upsert_mail_evidence(session, draft)
    links = match_evidence_to_transactions(session, evidence)
    session.commit()
    session.refresh(evidence)
    for link in links:
        session.refresh(link)
    return evidence, links
