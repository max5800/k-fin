"""Pydantic models for the Santander Consumer Bank credit-card API (M16-P2c).

These project a Santander MySantander credit-card response onto the flat
fields the canonical adapter (:mod:`src.normalization.santander_canonicalize`)
needs — nothing more. The exact upstream field names are not publicly
documented; the spike (``docs/integrations/santander.md``) builds against an
*assumed* contract and the models accept a few documented aliases so a
Live-Capture only has to widen the alias set, never restructure.

Per credit-card transaction we bind:

* identity — ``transaction_id`` (Santander's stable id) **or** ``None`` when
  the source omits one (the canonical adapter then derives a deterministic
  hash over booking_date / amount / merchant / card_last4);
* timing — ``booking_date``, ``valuation_date``;
* counterparty — ``merchant_name``, ``merchant_country``;
* money — ``amount`` + ``currency`` (the EUR settlement amount, signed:
  a purchase is negative), plus ``original_amount`` + ``original_currency``
  for foreign-currency purchases (FX — the common case on a travel card);
* card — ``card_last4``.

Every model is ``extra="ignore"``: the upstream envelope drifts and is
version-unstable, so only the documented subset is bound. Missing *required*
fields raise a ``pydantic.ValidationError`` — the connector turns that into a
clear :class:`~src.external.santander_client.SantanderParseError` rather than
letting a structure drift pass silently.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

_CONFIG = ConfigDict(extra="ignore", populate_by_name=True)


class SantanderCard(BaseModel):
    """One credit card owned by the user (from the card-list endpoint)."""

    model_config = _CONFIG

    card_id: str = Field(validation_alias="id")
    #: Last four PAN digits — never the full number (the connector never
    #: requests or stores a full card number).
    last4: str = Field(validation_alias="maskedNumber")
    product_name: str | None = Field(default=None, validation_alias="productName")


class SantanderTransaction(BaseModel):
    """One credit-card transaction in the assumed MySantander shape.

    ``transaction_id`` is optional by design: Santander does not guarantee a
    stable per-booking id, so the canonical adapter falls back to a
    deterministic content hash when it is absent.
    """

    model_config = _CONFIG

    transaction_id: str | None = Field(default=None, validation_alias="id")
    booking_date: date = Field(validation_alias="bookingDate")
    valuation_date: date | None = Field(default=None, validation_alias="valueDate")

    merchant_name: str | None = Field(default=None, validation_alias="merchant")
    #: ISO-3166 alpha-2 country of the merchant, when the source supplies it.
    merchant_country: str | None = Field(
        default=None, validation_alias="merchantCountry"
    )

    #: Signed EUR settlement amount — a purchase is negative, a refund/credit
    #: positive. This is the amount that hits the card balance.
    amount: Decimal = Field(validation_alias="amount")
    currency: str = Field(default="EUR", validation_alias="currency")

    #: Foreign-currency leg of an FX purchase. Both are ``None`` for a plain
    #: EUR transaction. ``original_amount`` carries the same sign as ``amount``.
    original_amount: Decimal | None = Field(
        default=None, validation_alias="originalAmount"
    )
    original_currency: str | None = Field(
        default=None, validation_alias="originalCurrency"
    )

    #: Last four PAN digits of the card the transaction was booked on.
    card_last4: str | None = Field(default=None, validation_alias="cardLast4")

    #: True while the transaction is an authorisation that has not yet been
    #: finally booked (``supports_pending_bookings`` — see the spike §9).
    pending: bool = Field(default=False, validation_alias="pending")

    @property
    def is_fx(self) -> bool:
        """True when the transaction settled from a non-EUR original amount."""
        return bool(
            self.original_currency
            and self.original_currency.upper() != (self.currency or "EUR").upper()
        )
