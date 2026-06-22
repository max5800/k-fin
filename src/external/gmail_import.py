"""Read-only Gmail message conversion for mail evidence imports.

This module accepts Gmail API ``users.messages.get(format=full)`` payloads and
turns them into the provider-neutral ``MailMessageImport`` shape. It does not
call Gmail itself and it does not persist raw mail content.
"""

from __future__ import annotations

import base64
import html
import re
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from typing import Any


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_DROP_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)
_HTML_BREAK_RE = re.compile(r"<\s*(br|/p|/div|/tr|/li)\b[^>]*>", re.I)
_SPACE_RE = re.compile(r"\s+")


def _decode_body(data: str | None) -> str:
    if not data:
        return ""
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(f"{data}{padding}").decode("utf-8", errors="replace")


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    values: dict[str, str] = {}
    for header in payload.get("headers") or []:
        name = str(header.get("name") or "").lower()
        value = str(header.get("value") or "")
        if name and value:
            values[name] = value
    return values


def _walk_parts(part: dict[str, Any]) -> list[dict[str, Any]]:
    parts = [part]
    for child in part.get("parts") or []:
        parts.extend(_walk_parts(child))
    return parts


def _html_to_text(value: str) -> str:
    value = _HTML_DROP_RE.sub(" ", value)
    value = _HTML_BREAK_RE.sub("\n", value)
    without_tags = _HTML_TAG_RE.sub(" ", value)
    return _SPACE_RE.sub(" ", html.unescape(without_tags)).strip()


def _message_body(payload: dict[str, Any]) -> str:
    plain_chunks: list[str] = []
    html_chunks: list[str] = []
    for part in _walk_parts(payload):
        mime_type = str(part.get("mimeType") or "").lower()
        decoded = _decode_body((part.get("body") or {}).get("data"))
        if not decoded:
            continue
        if mime_type == "text/plain":
            plain_chunks.append(decoded)
        elif mime_type == "text/html":
            html_chunks.append(_html_to_text(decoded))

    if plain_chunks:
        return "\n".join(chunk.strip() for chunk in plain_chunks if chunk.strip())
    return "\n".join(chunk for chunk in html_chunks if chunk)


def _received_at(message: dict[str, Any], headers: dict[str, str]) -> date | None:
    internal_date = message.get("internalDate")
    if internal_date:
        try:
            return datetime.fromtimestamp(int(internal_date) / 1000, tz=UTC).date()
        except (TypeError, ValueError, OSError):
            pass

    header_date = headers.get("date")
    if header_date:
        try:
            parsed = parsedate_to_datetime(header_date)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.date()
        except (TypeError, ValueError, IndexError, OverflowError):
            pass
    return None


def gmail_message_to_mail_import(message: dict[str, Any]) -> dict[str, Any]:
    """Convert a Gmail API message payload into ``MailMessageImport`` data."""

    payload = message.get("payload") or {}
    headers = _headers(payload)
    body_text = _message_body(payload)
    return {
        "source": "gmail",
        "source_message_id": str(message["id"]),
        "received_at": _received_at(message, headers),
        "sender": headers.get("from"),
        "subject": headers.get("subject") or "(no subject)",
        "body_text": body_text,
    }
