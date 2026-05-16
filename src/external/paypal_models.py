"""Pydantic models for the PayPal Transaction Search API.

M16-P2b. These project the deeply-nested ``/v1/reporting/transactions``
payload onto the flat fields the canonical adapter
(:mod:`src.normalization.paypal_canonicalize`) needs — nothing more.

Mapped, per ``transaction_details[]`` element:

* ``transaction_info`` — ``transaction_id``, ``transaction_initiation_date``,
  ``transaction_amount.value`` / ``currency_code``, ``transaction_event_code``,
  ``transaction_subject`` / ``transaction_note``;
* ``payer_info`` — ``email_address``, ``payer_name`` (given / surname /
  ``alternate_full_name`` — the latter carries the business name);
* ``cart_info`` — ``item_details[].item_name`` (the per-line merchandise).

Every model is ``extra="ignore"``: PayPal returns a large, version-drifting
envelope and we deliberately bind only the documented subset.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

_CONFIG = ConfigDict(extra="ignore", populate_by_name=True)


class PayPalAmount(BaseModel):
    """A signed monetary amount. PayPal pre-signs ``value``: a payment
    sent is negative, money received is positive — same convention as the
    canonical ``amount`` field, so no sign flip is needed downstream."""

    model_config = _CONFIG

    value: Decimal = Decimal("0")
    currency_code: str = "EUR"


class PayPalPayerName(BaseModel):
    """Counterparty name. ``alternate_full_name`` is the business/display
    name PayPal shows for merchants; given/surname is the personal name."""

    model_config = _CONFIG

    given_name: str | None = None
    surname: str | None = None
    alternate_full_name: str | None = None


class PayPalPayerInfo(BaseModel):
    """The transaction counterparty (PayPal's ``payer_info`` block)."""

    model_config = _CONFIG

    account_id: str | None = None
    email_address: str | None = None
    payer_name: PayPalPayerName | None = None


class PayPalItem(BaseModel):
    """One line item from the cart (``cart_info.item_details[]``)."""

    model_config = _CONFIG

    item_name: str | None = None
    item_description: str | None = None
    item_quantity: str | None = None
    item_amount: PayPalAmount | None = None


class PayPalCartInfo(BaseModel):
    """The cart/merchandise block — present for purchases, absent for
    bank funding / withdrawal transactions."""

    model_config = _CONFIG

    item_details: list[PayPalItem] = Field(default_factory=list)


class PayPalTransactionInfo(BaseModel):
    """The core ``transaction_info`` block — identity, amount, timing."""

    model_config = _CONFIG

    transaction_id: str
    transaction_event_code: str | None = None
    transaction_initiation_date: str | None = None
    transaction_updated_date: str | None = None
    transaction_amount: PayPalAmount = Field(default_factory=PayPalAmount)
    fee_amount: PayPalAmount | None = None
    transaction_status: str | None = None
    transaction_subject: str | None = None
    transaction_note: str | None = None


class PayPalTransaction(BaseModel):
    """One ``transaction_details[]`` element from the reporting API."""

    model_config = _CONFIG

    transaction_info: PayPalTransactionInfo
    payer_info: PayPalPayerInfo | None = None
    cart_info: PayPalCartInfo | None = None
