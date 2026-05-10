"""Refund-audit heuristic — backbone of the M9-Iter accounting (2026-05-08).

Walks "Erstattungen" income-bucket rows and decides whether each one is

  * a genuine **refund** (Krankenkasse, Splitwise, retailer return) — flip
    ``is_refund=True``, swap the category onto the original-expense bucket,
    stamp the audit decision so it never re-surfaces in the queue;
  * **real income** (Finanzamt, Cashback, Zinsen) — leave on
    ``erstattungen`` but stamp the audit decision so the row drops out of
    the manual review queue;
  * **uncertain** — leave untouched for the user to decide via the audit UI.

Three call-sites consume this service:

  * :func:`src.api.app._run_refund_audit_startup` — boot-time auto-apply
    pass run from the FastAPI lifespan hook;
  * :func:`src.api.routers.aggregates.refund_audit` — read-only candidate
    listing for the UI's manual review queue;
  * :func:`src.api.routers.aggregates.refund_audit_auto_apply` — manual
    re-trigger endpoint (user-only auth).

Everything in here is pure SQLAlchemy-on-Session — no FastAPI, no auth,
no schema mapping. Routers stay thin and testable.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.db.categories import INCOME_CATCHALL_CATEGORY_ID
from src.core.db.models import NormalizedTransaction

# Sender/recipient/description keywords → likely original-expense category.
# Order matters: the *first* matching pattern wins, so put the most specific
# signals first. Each tuple is (substring, suggested_category_id, reason,
# auto_apply). `auto_apply=True` means the keyword is unambiguous enough that
# the backend silently flips the row (is_refund=true, category swapped, audit
# stamped) — same posture as the categorization agent's high-confidence path.
# `auto_apply=False` means the keyword is suggestive but the row should land
# in the user-facing audit queue for manual confirmation.
REFUND_HEURISTICS: tuple[tuple[str, str | None, str, bool], ...] = (
    # ---- Healthcare: highly specific senders → auto-apply -----------------
    ("techniker krankenk", "gesundheit", "TK-Erstattung", True),
    ("krankenkasse", "gesundheit", "Krankenkassen-Erstattung", True),
    ("aok", "gesundheit", "AOK-Erstattung", True),
    ("barmer", "gesundheit", "Barmer-Erstattung", True),
    ("apotheke", "gesundheit", "Apotheken-Rückzahlung", True),
    ("zahnarzt", "gesundheit", "Zahnarzt-Erstattung", True),
    # ---- Travel: clear vendor → auto-apply --------------------------------
    ("booking.com", "reisen", "Booking-Stornierung", True),
    ("airbnb", "reisen", "Airbnb-Erstattung", True),
    ("lufthansa", "reisen", "Lufthansa-Refund", True),
    ("deutsche bahn", "reisen", "Bahn-Erstattung", True),
    # ---- Clothing retailers: well-known → auto-apply ----------------------
    ("zalando", "kleidung", "Zalando-Retoure", True),
    ("about you", "kleidung", "About You Retoure", True),
    # ---- Ambiguous patterns: review queue keeps them ----------------------
    # Splitwise tx default to restaurant-cafe but could be groceries / travel
    # / shared rent — show in audit so user picks the right cat.
    ("splitwise", "restaurant-cafe", "Splitwise-Ausgleich (Kategorie prüfen)", False),
    # Multi-token patterns: every space-separated token must appear in the
    # haystack (PayPal-Friends booking text varies — "PayPal *Anna Müller
    # Friends*", "PAYPAL.Anna Müller.Friends", etc.).
    ("paypal friends", "restaurant-cafe", "PayPal-Friends-Ausgleich", False),
    # Amazon spans elektronik / kleidung / haushalt — heuristic guesses
    # kleidung but the user should confirm.
    ("amazon", "kleidung", "Amazon-Retoure (Kategorie prüfen)", False),
    ("retoure", "kleidung", "Retoure", False),
    ("rückerstattung", "kleidung", "Rückerstattung", False),
    ("spesen", "reisen", "Arbeitgeber-Spesen", False),
    ("reisekosten", "reisen", "Reisekosten-Erstattung", False),
    # ---- Real-income patterns: leave is_refund=False, but auto-stamp the
    # audit so the row drops out of the review queue. Specific senders only
    # — generic "steuer"/"bonus" stay in audit because they false-match too
    # easily (rent description "Hausrat-Bonus", etc.).
    ("finanzamt", None, "Steuerrückzahlung — bleibt Einkommen", True),
    ("cashback", None, "Cashback — bleibt Einkommen", True),
    ("zinsen", None, "Zinsgutschrift — bleibt Einkommen", True),
    ("dividende", None, "Dividende — bleibt Einkommen", True),
    ("steuer", None, "Steuerbezug — bleibt Einkommen", False),
    ("bonus", None, "Bonus — bleibt Einkommen", False),
)


def suggest_refund_category(
    sender: str | None, recipient: str | None, description: str | None
) -> tuple[str | None, str | None, bool]:
    """Best-effort guess for the original expense category of a refund.

    Returns ``(category_id, reason, auto_apply)``. ``category_id is None``
    signals a "real income" pattern (the UI / auto-applier just stamps
    the audit decision without changing category). ``auto_apply`` is
    True when the heuristic match is unambiguous enough to skip user
    review.
    """
    haystack = " ".join(
        s for s in (sender or "", recipient or "", description or "") if s
    ).lower()
    if not haystack:
        return (None, None, False)
    for needle, suggested, reason, auto_apply in REFUND_HEURISTICS:
        # A space in the needle means "all tokens must be present" — lets
        # us match e.g. "PayPal *Anna Müller* Friends" without writing a
        # full regex.
        tokens = needle.split()
        if all(tok in haystack for tok in tokens):
            return (suggested, reason, auto_apply)
    return (None, None, False)


def _undecided_erstattungen_query():
    """SELECT statement returning every refund-audit candidate row.

    Shared between the auto-applier (which mutates) and the read-only
    candidate listing (which just maps to the response model). A
    *candidate* is a positive-amount transaction currently sitting in
    the ``erstattungen`` income bucket with ``is_refund=False`` and no
    audit decision yet.
    """
    return (
        select(NormalizedTransaction)
        .where(NormalizedTransaction.category_id == INCOME_CATCHALL_CATEGORY_ID)
        .where(NormalizedTransaction.is_refund.is_(False))
        .where(NormalizedTransaction.amount > 0)
        .where(NormalizedTransaction.internal_transfer.is_(False))
        .where(NormalizedTransaction.refund_audit_decided_at.is_(None))
    )


def list_audit_candidates(session: Session) -> list[NormalizedTransaction]:
    """Read-only: return all undecided refund-audit candidates.

    Ordered newest-first so the UI lands on the most relevant rows. The
    caller maps :class:`NormalizedTransaction` rows onto the response
    schema (and runs :func:`suggest_refund_category` per row).
    """
    return list(
        session.execute(
            _undecided_erstattungen_query().order_by(
                NormalizedTransaction.booking_date.desc()
            )
        )
        .scalars()
        .all()
    )


def apply_refund_heuristic(session: Session) -> dict[str, int]:
    """Walk all undecided erstattungen-Tx, auto-apply the high-confidence ones.

    Returns counts: ``{"applied_refund": int, "applied_income": int,
    "left_for_review": int}``. Idempotent — re-running on a cleaned DB
    flips nothing. Safe to call from the API lifespan, the worker post-
    sync hook, and via the manual ``/refund-audit/auto-apply`` endpoint.
    """
    rows = session.execute(_undecided_erstattungen_query()).scalars().all()

    now = datetime.now(timezone.utc)
    applied_refund = applied_income = left_for_review = 0
    for tx in rows:
        suggested, _reason, auto_apply = suggest_refund_category(
            tx.sender, tx.recipient, tx.description
        )
        if not auto_apply:
            left_for_review += 1
            continue
        if suggested is not None:
            tx.category_id = suggested
            tx.is_refund = True
            applied_refund += 1
        else:
            # "Real income" pattern (Finanzamt, Cashback, …) — stamp only.
            applied_income += 1
        tx.refund_audit_decided_at = now

    if applied_refund or applied_income:
        session.commit()
    return {
        "applied_refund": applied_refund,
        "applied_income": applied_income,
        "left_for_review": left_for_review,
    }
