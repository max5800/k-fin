"""Pydantic models for Comdirect API responses.

These models parse the raw nested dicts from the Comdirect REST API
into flat, typed objects. All downstream code (exporters, importers,
scripts) should consume these models instead of navigating raw dicts.
"""

from pydantic import BaseModel, Field, model_validator


class ComdirectAccount(BaseModel):
    """A bank account (Giro, Tagesgeld, Verrechnungskonto)."""

    account_id: str = ""
    iban: str = ""
    account_type: str = ""
    balance: float = 0.0
    currency: str = "EUR"

    @model_validator(mode="before")
    @classmethod
    def _unwrap_nested(cls, data: dict) -> dict:
        """Handle both flat and nested Comdirect response shapes."""
        if not isinstance(data, dict):
            return data
        inner = data.get("account") or data
        balance_raw = data.get("balance") or inner.get("balance") or {}
        account_type_raw = (
            inner.get("accountType", {}).get("key")
            if isinstance(inner.get("accountType"), dict)
            else inner.get("accountType", "")
        )
        return {
            "account_id": inner.get("accountId") or data.get("accountId") or "",
            "iban": inner.get("iban") or data.get("iban") or "",
            "account_type": account_type_raw or "",
            "balance": (
                float(balance_raw.get("value") or 0)
                if isinstance(balance_raw, dict)
                else float(balance_raw or 0)
            ),
            "currency": (
                balance_raw.get("unit", "EUR") if isinstance(balance_raw, dict) else "EUR"
            ),
        }


class ComdirectTransaction(BaseModel):
    """A bank account transaction (Umsatz)."""

    transaction_id: str = ""
    booking_date: str = ""
    value_date: str = ""
    amount: float = 0.0
    currency: str = "EUR"
    type_text: str = ""
    remittance_info: str = ""
    creditor_name: str = ""
    creditor_iban: str = ""
    debtor_name: str = ""
    debtor_iban: str = ""

    @model_validator(mode="before")
    @classmethod
    def _unwrap_nested(cls, data: dict) -> dict:
        if not isinstance(data, dict):
            return data
        tx_value = data.get("transactionValue") or data.get("amount") or {}
        creditor = data.get("creditor") or {}
        debtor = data.get("debtor") or {}

        # Extractor for either native flat float or dict
        if isinstance(tx_value, dict):
            amount = float(tx_value.get("value") or 0)
            currency = tx_value.get("unit") or "EUR"
        else:
            amount = float(tx_value)
            currency = data.get("currency") or "EUR"

        return {
            "transaction_id": data.get("transactionId") or data.get("transaction_id") or "",
            "booking_date": data.get("bookingDate") or data.get("booking_date") or "",
            "value_date": data.get("valutaDate") or data.get("value_date") or "",
            "amount": amount,
            "currency": currency,
            "type_text": data.get("typeText") or data.get("type_text") or "",
            "remittance_info": data.get("remittanceInfo") or data.get("remittance_info") or "",
            "creditor_name": creditor.get("holderName") or data.get("creditor_name") or "",
            "creditor_iban": creditor.get("iban") or data.get("creditor_iban") or "",
            "debtor_name": debtor.get("holderName") or data.get("debtor_name") or "",
            "debtor_iban": debtor.get("iban") or data.get("debtor_iban") or "",
        }

    @property
    def is_debit(self) -> bool:
        return self.amount < 0

    @property
    def counterpart_name(self) -> str:
        return self.creditor_name if self.is_debit else self.debtor_name

    @property
    def counterpart_iban(self) -> str:
        return self.creditor_iban if self.is_debit else self.debtor_iban

    @property
    def description(self) -> str:
        return (self.remittance_info or self.type_text or "Umsatz")[:255]


class DepotPosition(BaseModel):
    """A depot holding (Wertpapierposition)."""

    isin: str = ""
    wkn: str = ""
    name: str = ""
    instrument_type: str = ""
    quantity: float = 0.0
    current_price: float = 0.0
    current_value: float = 0.0
    purchase_value: float = 0.0
    prev_day_price: float = 0.0
    prev_day_value: float = 0.0
    currency: str = "EUR"

    @model_validator(mode="before")
    @classmethod
    def _unwrap_nested(cls, data: dict) -> dict:
        if not isinstance(data, dict):
            return data
        instrument = data.get("instrument") or {}
        return {
            "isin": instrument.get("isin") or data.get("isin") or "",
            "wkn": instrument.get("wkn") or data.get("wkn") or "",
            "name": instrument.get("name") or data.get("name") or "",
            "instrument_type": (
                instrument.get("instrumentType")
                or data.get("instrumentType")
                or ""
            ),
            "quantity": float(data.get("quantity", {}).get("value") or data.get("stueckzahl") or 0),
            "current_price": float(
                data.get("currentPrice", {}).get("price", {}).get("value")
                or data.get("kurs", {}).get("value")
                or 0
            ),
            "current_value": float(
                data.get("currentValue", {}).get("value")
                or data.get("kurswert", {}).get("value")
                or 0
            ),
            "purchase_value": float(
                data.get("purchaseValue", {}).get("value")
                or data.get("einstandswert", {}).get("value")
                or 0
            ),
            "prev_day_price": float(
                data.get("prevDayPrice", {}).get("price", {}).get("value")
                or data.get("prevDayPrice", {}).get("value")
                or 0
            ),
            "prev_day_value": float(
                data.get("prevDayValue", {}).get("value") or 0
            ),
            "currency": (
                data.get("currentValue", {}).get("unit")
                or data.get("kurswert", {}).get("unit")
                or "EUR"
            ),
        }

    @property
    def gains(self) -> float:
        return round(self.current_value - self.purchase_value, 2)

    @property
    def gains_percent(self) -> float:
        return round((self.gains / self.purchase_value * 100) if self.purchase_value else 0, 4)

    @property
    def daily_gains(self) -> float:
        """Today's P&L vs. previous-day close. Uses prevDayValue when available,
        falls back to prev_day_price × quantity."""
        if self.prev_day_value:
            return round(self.current_value - self.prev_day_value, 2)
        if self.prev_day_price:
            return round((self.current_price - self.prev_day_price) * self.quantity, 2)
        return 0.0

    @property
    def daily_gains_percent(self) -> float:
        ref = self.prev_day_value or (self.prev_day_price * self.quantity)
        return round((self.daily_gains / ref * 100) if ref else 0, 4)


class DepotTransaction(BaseModel):
    """A depot transaction (Kauf, Verkauf, Dividende)."""

    transaction_id: str = ""
    booking_date: str = ""
    isin: str = ""
    wkn: str = ""
    name: str = ""
    transaction_type: str = ""
    quantity: float = 0.0
    price: float = 0.0
    amount: float = 0.0
    currency: str = "EUR"

    _DEPOT_TYPE_MAP: dict[str, str] = {
        "BUY": "BUY",
        "IN": "BUY",
        "KAUF": "BUY",
        "SELL": "SELL",
        "OUT": "SELL",
        "VERKAUF": "SELL",
        "DIVIDEND": "DIVIDEND",
        "ERTRAG": "DIVIDEND",
    }

    @model_validator(mode="before")
    @classmethod
    def _unwrap_nested(cls, data: dict) -> dict:
        if not isinstance(data, dict):
            return data
        instrument = data.get("instrument") or {}
        raw_type = (data.get("transactionType") or data.get("transactionDirection") or "").upper()
        type_map = {
            "BUY": "BUY",
            "IN": "BUY",
            "KAUF": "BUY",
            "SELL": "SELL",
            "OUT": "SELL",
            "VERKAUF": "SELL",
            "DIVIDEND": "DIVIDEND",
            "ERTRAG": "DIVIDEND",
        }
        return {
            "transaction_id": data.get("transactionId") or "",
            "booking_date": data.get("bookingDate") or data.get("transactionDate") or "",
            "isin": instrument.get("isin") or data.get("isin") or "",
            "wkn": instrument.get("wkn") or data.get("wkn") or "",
            "name": instrument.get("name") or data.get("name") or "",
            "transaction_type": type_map.get(raw_type, "OTHER"),
            "quantity": float(data.get("quantity", {}).get("value") or data.get("stueckzahl") or 0),
            "price": float(
                data.get("price", {}).get("value") or data.get("kurs", {}).get("value") or 0
            ),
            "amount": float(
                data.get("transactionValue", {}).get("value")
                or data.get("amount", {}).get("value")
                or 0
            ),
            "currency": (
                data.get("transactionValue", {}).get("unit")
                or data.get("amount", {}).get("unit")
                or "EUR"
            ),
        }


class ComdirectData(BaseModel):
    """Complete dataset from ComdirectClient.get_all_data()."""

    accounts: list[ComdirectAccount] = Field(default_factory=list)
    transactions: dict[str, list[ComdirectTransaction]] = Field(default_factory=dict)
    depots: list[dict] = Field(default_factory=list)
    depot_positions: dict[str, list[DepotPosition]] = Field(default_factory=dict)
    depot_transactions: dict[str, list[DepotTransaction]] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _parse_nested(cls, data: dict) -> dict:
        if not isinstance(data, dict):
            return data
        return {
            "accounts": [ComdirectAccount.model_validate(a) for a in (data.get("accounts") or [])],
            "transactions": {
                k: [ComdirectTransaction.model_validate(tx) for tx in v]
                for k, v in (data.get("transactions") or {}).items()
            },
            "depots": data.get("depots") or [],
            "depot_positions": {
                k: [DepotPosition.model_validate(p) for p in v]
                for k, v in (data.get("depot_positions") or {}).items()
            },
            "depot_transactions": {
                k: [DepotTransaction.model_validate(tx) for tx in v]
                for k, v in (data.get("depot_transactions") or {}).items()
            },
        }
