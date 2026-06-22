#!/usr/bin/env python
"""Import read-only Gmail messages into k-fin mail evidence.

The script shells out to ``gws`` for Gmail reads, converts each Gmail payload to
the mail-evidence import shape, and posts it to k-fin. It never writes to Gmail.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib import error, request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.external.gmail_import import gmail_message_to_mail_import  # noqa: E402
from src.services.mail_evidence import extract_evidence_from_message, redact_free_text  # noqa: E402

DEFAULT_QUERY = (
    "newer_than:45d "
    "(rechnung OR invoice OR quittung OR receipt OR bestellung OR order OR zahlung OR payment OR refund OR erstattung)"
)


def _run_gws(args: list[str]) -> dict[str, Any]:
    completed = subprocess.run(
        ["gws", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gws returned non-JSON output: {completed.stdout[:200]}") from exc


def _list_message_ids(query: str, max_results: int) -> list[str]:
    payload = _run_gws(
        [
            "gmail",
            "users",
            "messages",
            "list",
            "--params",
            json.dumps({"userId": "me", "q": query, "maxResults": max_results}),
        ]
    )
    messages = payload.get("messages") or []
    return [str(message["id"]) for message in messages if message.get("id")]


def _fetch_message(message_id: str) -> dict[str, Any]:
    return _run_gws(
        [
            "gmail",
            "users",
            "messages",
            "get",
            "--params",
            json.dumps({"userId": "me", "id": message_id, "format": "full"}),
        ]
    )


def _post_import(api_url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload, default=str).encode("utf-8")
    req = request.Request(
        f"{api_url.rstrip('/')}/api/v1/mail-evidence/import",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default=os.getenv("FINANCE_API_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--token", default=os.getenv("FINANCE_API_TOKEN"))
    parser.add_argument("--query", default=os.getenv("GMAIL_QUERY", DEFAULT_QUERY))
    parser.add_argument("--max-results", type=int, default=int(os.getenv("GMAIL_MAX_RESULTS", "25")))
    parser.add_argument("--min-confidence", default=os.getenv("GMAIL_MIN_CONFIDENCE", "0.50"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.dry_run and not args.token:
        parser.error("--token or FINANCE_API_TOKEN is required unless --dry-run is set")

    message_ids = _list_message_ids(args.query, args.max_results)
    imported = 0
    matched = 0
    skipped = 0
    skipped_low_confidence = 0
    for message_id in message_ids:
        gmail_payload = _fetch_message(message_id)
        mail_import = gmail_message_to_mail_import(gmail_payload)
        if not mail_import["body_text"]:
            skipped += 1
            continue
        draft = extract_evidence_from_message(mail_import)
        if float(draft["confidence"]) < float(args.min_confidence) or draft["total_amount"] is None:
            skipped_low_confidence += 1
            continue
        if args.dry_run:
            print(
                json.dumps(
                    {
                        "source_message_id": mail_import["source_message_id"],
                        "received_at": mail_import["received_at"],
                        "sender": redact_free_text(mail_import["sender"], limit=120),
                        "subject_hint": redact_free_text(mail_import["subject"], limit=160),
                        "evidence_type": draft["evidence_type"],
                        "merchant_key": draft["merchant_key"],
                        "total_amount": draft["total_amount"],
                        "confidence": draft["confidence"],
                        "body_chars": len(mail_import["body_text"]),
                    },
                    default=str,
                    ensure_ascii=False,
                )
            )
            continue
        try:
            result = _post_import(args.api_url, args.token, mail_import)
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"k-fin import failed for {message_id}: {exc.code} {detail}") from exc
        imported += 1
        matched += len(result.get("links") or [])

    print(
        json.dumps(
            {
                "query": args.query,
                "seen": len(message_ids),
                "imported": imported,
                "matched_links": matched,
                "skipped_empty_body": skipped,
                "skipped_low_confidence": skipped_low_confidence,
                "dry_run": args.dry_run,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
