"""PayPal account-statement ("Kontoauszug") CSV importer — parser + adapter.

The maintainer's PayPal account is a *personal* account; PayPal's
Transaction Search REST API needs a *business* account, so k-fin ingests
PayPal data from the official **"Kontoauszug" CSV export** instead
(personal accounts can export up to seven years of history).

The Kontoauszug is a balance *ledger*: every real payment is recorded
together with one or more **funding rows** that move money into the
PayPal balance to cover it — a bank debit ("Bankgutschrift auf
PayPal-Konto"), a credit-card charge, a buyer-credit draw, an FX
conversion leg, an authorisation hold, and so on. Those funding rows are
PayPal-internal plumbing, not independent transactions; importing them
verbatim would invent phantom income and double-count every spend.

So this module imports **only the real transactions** — the rows that
carry a genuine counterparty in the ``Name`` column (a merchant or a
person). A row with an empty ``Name``, or a ``Name`` that is PayPal
itself, is a funding/plumbing row and is skipped (see
:func:`_is_real_transaction`). A rare real payment PayPal recorded
without a name is skipped too — an accepted v1 trade-off.

Two responsibilities:

* :func:`parse_paypal_csv` — the bytes of a downloaded CSV → a list of
  flat, JSON-safe row dicts, one per *real* transaction (each stored
  verbatim as one ``raw_transactions.raw_data``).
* :func:`paypal_csv_canonicalize` — one such row dict → the ten canonical
  identity fields shared with the Comdirect / Santander adapters.

**Defensive parsing.** Columns are mapped **by header name, never by
position** — a missing *required* column raises
:class:`PayPalCsvParseError` naming it, never a silent empty result
(mirrors the drift discipline of :mod:`src.external.santander_client`).

``external_id`` is PayPal's ``Transaktionscode`` — a stable, unique
per-transaction id, so ``(source, external_id)`` scoping makes a
re-uploaded overlapping export idempotent at ingest time.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from src.core.logging import get_logger
from src.normalization.canonicalize import _parse_date, _to_decimal

logger = get_logger("paypal-csv")


class PayPalCsvError(RuntimeError):
    """Base class for every PayPal-CSV import failure."""


class PayPalCsvParseError(PayPalCsvError):
    """The CSV did not match the expected structure — a missing required
    column, or a row with an unparseable date/amount (likely a schema
    drift or a wrong file). Raised loudly rather than silently dropping
    financial data."""


# Localized PayPal "Kontoauszug" header (case-folded) → stable internal
# key. ``Beschreibung`` is the PayPal transaction-type label; ``Entgelt``
# is the fee (older exports shipped it as ``Gebühr`` — both map to fee).
_HEADER_ALIASES: dict[str, str] = {
    "datum": "date",
    "uhrzeit": "time",
    "zeitzone": "timezone",
    "beschreibung": "description_type",
    "währung": "currency",
    "brutto": "gross",
    "entgelt": "fee",
    "gebühr": "fee",
    "netto": "net",
    "guthaben": "balance",
    "transaktionscode": "transaction_id",
    "absender e-mail-adresse": "from_email",
    "empfänger e-mail-adresse": "to_email",
    "name": "name",
    "name der bank": "bank_name",
    "bankkonto": "bank_account",
    "versand- und bearbeitungsgebühr": "shipping_fee",
    "umsatzsteuer": "vat",
    "rechnungsnummer": "invoice_number",
    "zugehöriger transaktionscode": "related_transaction_id",
}

# Without these a row cannot be canonicalized, deduplicated, or classified.
_REQUIRED_KEYS: tuple[str, ...] = (
    "date",
    "transaction_id",
    "gross",
    "currency",
    "name",
)

# Internal key → the German header to name in an error message.
_KEY_LABELS: dict[str, str] = {
    "date": "Datum",
    "transaction_id": "Transaktionscode",
    "gross": "Brutto",
    "currency": "Währung",
    "name": "Name",
}

# A foreign-currency payment is booked in its own currency; PayPal
# records the EUR cost as a separate row with this `Beschreibung`,
# linked back to the payment via `Zugehöriger Transaktionscode`.
# Matched case-folded.
_FX_CONVERSION_LABEL = "allgemeine währungsumrechnung"

# "PAYPAL *STEAMGAMES" / "PP*STEAM" → "STEAMGAMES" — strip the processor
# prefix so the bare vendor reaches the rule haystack / categoriser.
# (Kontoauszug names are usually already clean; kept as a safety net.)
_PAYPAL_PREFIX_RE = re.compile(r"^\s*(?:paypal|pp)\s*\*\s*", re.IGNORECASE)


def strip_paypal_prefix(text: str | None) -> str | None:
    """Drop a leading ``PAYPAL *`` / ``PP*`` processor prefix from *text*."""
    if not text:
        return text
    return _PAYPAL_PREFIX_RE.sub("", text).strip() or None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_paypal_csv(raw: bytes) -> list[dict[str, Any]]:
    """Parse a downloaded PayPal Kontoauszug CSV into flat row dicts.

    Returns one JSON-safe dict per *real* transaction (funding/plumbing
    rows are skipped — see :func:`_is_real_transaction`), keyed by the
    stable internal names from :data:`_HEADER_ALIASES`. Dates are
    normalized to ISO ``YYYY-MM-DD`` and amounts to dot-decimal strings,
    so the dict round-trips cleanly through ``raw_transactions.raw_data``.

    Raises :class:`PayPalCsvParseError` if a required column is absent or
    a real row carries an unparseable date / amount — a loud failure
    beats silently losing rows of financial data.
    """
    text = _decode(raw)
    reader = csv.DictReader(io.StringIO(text), dialect=_sniff_dialect(text))
    header_map = _build_header_map(reader.fieldnames)

    # Materialise every row first: a foreign-currency payment needs its
    # FX conversion leg, which is a *separate* row — so emitting requires
    # a prior pass over the whole file (see `_harvest_eur_conversions`).
    all_rows: list[tuple[int, dict[str, Any]]] = []
    for lineno, raw_row in enumerate(reader, start=2):  # line 1 is the header
        mapped = {
            key: (raw_row.get(header) or "").strip()
            for header, key in header_map.items()
        }
        if any(mapped.values()):  # drop blank trailing lines
            all_rows.append((lineno, mapped))

    eur_conversions = _harvest_eur_conversions(all_rows)

    rows: list[dict[str, Any]] = []
    skipped = 0
    for lineno, mapped in all_rows:
        if not _is_real_transaction(mapped):
            skipped += 1  # a bank/FX/credit funding row — internal plumbing
            continue

        if not mapped.get("transaction_id"):
            raise PayPalCsvParseError(
                f"row {lineno}: no Transaktionscode — cannot identify the "
                "transaction (the export may have dropped that column)"
            )

        mapped["date"] = _parse_german_date(mapped.get("date", ""), lineno)
        mapped["gross"] = str(_parse_german_decimal(mapped.get("gross", ""), lineno))
        for optional in ("fee", "net"):
            if mapped.get(optional):
                mapped[optional] = str(
                    _parse_german_decimal(mapped[optional], lineno)
                )
        # Foreign-currency payment → attach the EUR amount PayPal
        # converted it to, so `paypal_csv_canonicalize` can book the
        # canonical `amount` in EUR and keep the original as FX context.
        if (mapped.get("currency") or "").upper() != "EUR":
            eur = eur_conversions.get(mapped.get("transaction_id", ""))
            if eur is not None:
                mapped["eur_gross"] = str(eur)
        rows.append(mapped)

    logger.info(
        "PayPal CSV parsed: %d transaction(s), %d funding/plumbing row(s) skipped",
        len(rows),
        skipped,
    )
    return rows


def _harvest_eur_conversions(
    all_rows: list[tuple[int, dict[str, Any]]],
) -> dict[str, Decimal]:
    """Map a payment's ``Transaktionscode`` → the EUR amount it cost.

    A foreign-currency payment is booked in its own currency. PayPal
    records the EUR cost as a separate "Allgemeine Währungsumrechnung"
    row in EUR that references the payment through
    ``Zugehöriger Transaktionscode``. This first pass collects those EUR
    legs so the payment row can be canonicalized in EUR.

    A conversion leg with an unparseable amount is skipped — the payment
    then stays in its original currency rather than failing the import.
    """
    conversions: dict[str, Decimal] = {}
    for lineno, mapped in all_rows:
        label = (mapped.get("description_type") or "").strip().lower()
        currency = (mapped.get("currency") or "").strip().upper()
        related = (mapped.get("related_transaction_id") or "").strip()
        if label != _FX_CONVERSION_LABEL or currency != "EUR" or not related:
            continue
        try:
            conversions[related] = _parse_german_decimal(
                mapped.get("gross", ""), lineno
            )
        except PayPalCsvParseError:
            continue
    return conversions


def _is_real_transaction(row: dict[str, Any]) -> bool:
    """Whether a Kontoauszug row is a real payment / receipt.

    The Kontoauszug pairs every real transaction with internal funding
    rows (a bank debit, an FX conversion leg, a buyer-credit draw, an
    authorisation hold …). A real transaction carries a genuine
    counterparty in ``Name``; a funding/plumbing row has an empty
    ``Name`` — or names PayPal itself (a buyer-credit settlement back to
    "PayPal (Europe) S.à r.l."). Importing the plumbing rows would invent
    phantom income and double-count, so they are skipped.
    """
    name = (row.get("name") or "").strip()
    if not name:
        return False
    return "paypal" not in name.lower()


def _decode(raw: bytes) -> str:
    """Decode CSV bytes, transparently stripping a UTF-8 BOM (PayPal
    exports the file as UTF-8-with-BOM)."""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PayPalCsvParseError(
            "file is not valid UTF-8 — is this really a PayPal CSV export?"
        ) from exc


def _sniff_dialect(text: str) -> type[csv.Dialect] | csv.Dialect:
    """Detect the delimiter (PayPal ships comma-delimited; a German
    locale export can be semicolon-delimited). Falls back to comma."""
    try:
        return csv.Sniffer().sniff(text[:4096], delimiters=",;")
    except csv.Error:
        return csv.excel


def _build_header_map(fieldnames: list[str] | None) -> dict[str, str]:
    """Map the CSV's actual header cells to internal keys.

    Raises :class:`PayPalCsvParseError` naming every missing required
    column, so a wrong/customized export fails with an actionable error.
    """
    if not fieldnames:
        raise PayPalCsvParseError("the CSV has no header row")

    header_map: dict[str, str] = {}
    for header in fieldnames:
        key = _HEADER_ALIASES.get((header or "").strip().lower())
        if key and key not in header_map.values():
            header_map[header] = key

    present = set(header_map.values())
    missing = [_KEY_LABELS[k] for k in _REQUIRED_KEYS if k not in present]
    if missing:
        raise PayPalCsvParseError(
            "PayPal CSV is missing required column(s): "
            + ", ".join(missing)
            + " — export a Kontoauszug with the standard column set"
        )
    return header_map


def _parse_german_date(value: str, lineno: int) -> str:
    """Parse a ``DD.MM.YYYY`` date cell into an ISO ``YYYY-MM-DD`` string."""
    value = (value or "").strip()
    try:
        return datetime.strptime(value, "%d.%m.%Y").date().isoformat()
    except ValueError as exc:
        raise PayPalCsvParseError(
            f"row {lineno}: unparseable date {value!r} (expected DD.MM.YYYY)"
        ) from exc


def _parse_german_decimal(value: str, lineno: int) -> Decimal:
    """Parse a German-formatted amount (``-1.234,56``) into a Decimal.

    German number format: ``.`` groups thousands, ``,`` is the decimal
    separator. An empty cell is treated as ``0``. The maintainer's PayPal
    account is German; amounts therefore always carry a decimal comma.
    The no-comma branch is a safety net for a stray whole-number cell.
    """
    s = (value or "").strip().replace("\xa0", "").replace(" ", "")
    if not s:
        return Decimal("0")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif s.count(".") > 1:
        # multiple dots, no comma → all dots are thousands separators
        s = s.replace(".", "")
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError) as exc:
        raise PayPalCsvParseError(
            f"row {lineno}: unparseable amount {value!r}"
        ) from exc


# ---------------------------------------------------------------------------
# Canonical mapping
# ---------------------------------------------------------------------------


def paypal_csv_canonicalize(raw: dict[str, Any]) -> dict[str, Any]:
    """Project one parsed PayPal transaction row onto the canonical fields.

    The output carries the ten shared identity fields plus the two FX
    columns (``original_amount`` / ``original_currency``) — the same
    shape as the Santander adapter — so hashing, ingest and the pipeline
    stay source-agnostic. PayPal carries no IBANs; the counterparty
    lands in ``sender`` / ``recipient`` as the merchant / payer name. A
    foreign-currency payment is booked in EUR via its conversion leg,
    with the original kept in the FX columns.
    """
    amount = _to_decimal(raw.get("gross"))
    currency = (raw.get("currency") or "EUR").upper()
    booking_date = _parse_date(raw.get("date"))

    # Foreign-currency payment: the row is booked in its own currency,
    # but `parse_paypal_csv` attached the EUR amount PayPal converted it
    # to (`eur_gross`). Book the canonical `amount` in EUR — everything
    # downstream (sums, the lump-debit reconciliation, the categoriser)
    # works in EUR — and keep the original in the FX columns, exactly
    # like the Santander adapter. With no conversion leg (e.g. paid from
    # a foreign PayPal balance) the row stays in its own currency.
    original_amount: Decimal | None = None
    original_currency: str | None = None
    eur_gross = raw.get("eur_gross")
    if currency != "EUR" and eur_gross not in (None, ""):
        original_amount = amount
        original_currency = currency
        amount = _to_decimal(eur_gross)
        currency = "EUR"

    counterparty = _counterparty(raw)
    description = _description(raw)

    # debit (amount < 0): money leaves the PayPal balance → counterparty
    # is the recipient. credit (>= 0): money arrives → counterparty is the
    # sender. IBANs are always None — PayPal has none.
    if amount < 0:
        sender, recipient = None, counterparty
    else:
        sender, recipient = counterparty, None

    return {
        "external_id": raw.get("transaction_id"),
        "booking_date": booking_date,
        "valuation_date": booking_date,
        "amount": amount,
        "currency": currency,
        "sender": sender,
        "recipient": recipient,
        "sender_iban": None,
        "recipient_iban": None,
        "description": description[:500] if description else None,
        # FX context — populated only for a converted foreign-currency
        # payment, None for native-EUR rows. Carried outside the ten
        # hash fields, same as the Santander adapter.
        "original_amount": original_amount,
        "original_currency": original_currency,
    }


def _counterparty(raw: dict[str, Any]) -> str | None:
    """Resolve the merchant / payer display name from ``Name`` (the
    sender e-mail is a defensive fallback — a real row always has a
    ``Name``, see :func:`_is_real_transaction`)."""
    name = strip_paypal_prefix((raw.get("name") or "").strip() or None)
    if name:
        return name
    return (raw.get("from_email") or "").strip() or None


def _description(raw: dict[str, Any]) -> str | None:
    """Canonical free-text — PayPal's transaction-type label
    (``Beschreibung``), or the invoice / order number as a fallback."""
    for key in ("description_type", "invoice_number"):
        value = (raw.get(key) or "").strip()
        if value:
            return value
    return None
