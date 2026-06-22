"""Sanitize context before it is sent to LLM agents."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_DE_IBAN_RE = re.compile(r"\bDE\d{20}\b")
_GENERIC_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")
_LONG_DIGITS_RE = re.compile(r"\b\d{9,}\b")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-_\.~+/=]{8,}")
_REFERENCE_RE = re.compile(
    r"(?i)\b(mandat|mandate|reference|referenz|endtoend|order|invoice)"
    r"[\s:#-]*[A-Z0-9][A-Z0-9._/-]{3,}\b"
)
_BANNED_KEY_RE = re.compile(
    r"(iban|raw|source_payload|account_id|depot_id|external_id|mandate|"
    r"reference|order|invoice|message_id|token|password|webhook|database_url|email)",
    re.I,
)


def sanitize_text(value: str, *, limit: int = 300) -> str:
    out = _EMAIL_RE.sub("[email]", value)
    out = _DE_IBAN_RE.sub("DE[iban]", out)
    out = _GENERIC_IBAN_RE.sub("[iban]", out)
    out = _BEARER_RE.sub("Bearer [token]", out)
    out = _REFERENCE_RE.sub(lambda m: f"{m.group(1)} [ref]", out)
    out = _LONG_DIGITS_RE.sub("[number]", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out[:limit]


class LLMContextSanitizer:
    def __init__(self) -> None:
        self._tx_map: dict[str, str] = {}
        self._evidence_map: dict[str, str] = {}

    def _pseudonym(self, value: Any, mapping: dict[str, str], prefix: str) -> str:
        key = str(value)
        if key not in mapping:
            mapping[key] = f"{prefix}_{len(mapping) + 1:03d}"
        return mapping[key]

    def sanitize(self, value: Any, *, key: str | None = None) -> Any:
        if key and _BANNED_KEY_RE.search(key):
            return None
        if key == "transaction_id":
            return self._pseudonym(value, self._tx_map, "tx")
        if key == "transaction_ids" and isinstance(value, Sequence) and not isinstance(value, str):
            return [self._pseudonym(item, self._tx_map, "tx") for item in value]
        if key == "evidence_id":
            return self._pseudonym(value, self._evidence_map, "ev")
        if isinstance(value, str):
            return sanitize_text(value)
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, datetime | date):
            return value.isoformat()
        if isinstance(value, Mapping):
            return {
                item_key: sanitized
                for item_key, item_value in value.items()
                for sanitized in [self.sanitize(item_value, key=str(item_key))]
                if sanitized is not None
            }
        if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
            return [self.sanitize(item) for item in value]
        return value

    @property
    def tx_map(self) -> dict[str, str]:
        return dict(self._tx_map)


def sanitize_context(context: Mapping[str, Any]) -> dict[str, Any]:
    sanitizer = LLMContextSanitizer()
    sanitized = sanitizer.sanitize(context)
    assert_context_safe(sanitized)
    return sanitized


def sanitize_search_query(query: str) -> str:
    safe = sanitize_text(query, limit=120)
    # Web search should get a merchant-ish phrase only, not a whole booking text.
    safe = re.sub(r"\[(?:email|iban|number|token|ref)\]", " ", safe, flags=re.I)
    safe = re.sub(r"[^A-Za-z0-9ÄÖÜäöüß .&+-]", " ", safe)
    safe = re.sub(r"\s+", " ", safe).strip()
    return safe[:80]


def assert_context_safe(context: Any) -> None:
    serialized = json.dumps(context, ensure_ascii=False, default=str)
    forbidden = [
        ("email", _EMAIL_RE),
        ("de_iban", _DE_IBAN_RE),
        ("generic_iban", _GENERIC_IBAN_RE),
        ("bearer_token", _BEARER_RE),
        ("long_digits", _LONG_DIGITS_RE),
        ("reference", _REFERENCE_RE),
    ]
    for label, pattern in forbidden:
        if pattern.search(serialized):
            raise ValueError(f"LLM context contains forbidden pattern: {label}")
