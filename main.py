"""comdirect-worker — internal sync service.

Runs on port 8001, never exposed to the internet.
Receives sync requests from the public comdirect-api service.
"""

import uuid

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from src.api.routers import transactions
from src.connector.comdirect_client import ComdirectClient
from src.core.config import settings
from src.core.logging import get_logger, setup_logging

setup_logging()
logger = get_logger("worker")

app = FastAPI(
    title="K-Fin Worker",
    description="Internal sync worker — not publicly accessible",
    version="0.2.0",
)

# ---------------------------------------------------------------------------
# In-memory pending sessions (single-worker deployment)
# ---------------------------------------------------------------------------

_pending_sessions: dict[str, dict] = {}

# Finance API (M6) — transaction endpoints
app.include_router(transactions.router, prefix="/api/v1")


class SyncStartRequest(BaseModel):
    account_transaction_limit: int | None = None
    account_transaction_min_booking_date: str | None = None
    depot_transaction_limit: int | None = None
    depot_transaction_min_booking_date: str | None = None


@app.get("/health")
def health():
    return {"status": "ok", "service": "comdirect-worker"}


@app.post("/internal/sync/start")
async def internal_sync_start(payload: SyncStartRequest | None = None):
    """Step 1: Begin auth flow and trigger TAN challenge."""
    client = ComdirectClient()

    try:
        auth_state = await client.begin_auth()
    except RuntimeError as exc:
        logger.error(f"begin_auth failed: {exc}")
        raise HTTPException(status_code=502, detail=str(exc))

    session_id = str(uuid.uuid4())
    _pending_sessions[session_id] = {
        "client": client,
        "session_identifier": auth_state["session_identifier"],
        "challenge_id": auth_state["challenge_id"],
        "config": (payload.model_dump(exclude_none=True) if payload else {}),
    }

    logger.info(f"TAN challenge sent, session_id={session_id}")
    return {"status": "pending_tan", "session_id": session_id}


@app.post("/internal/sync/confirm")
async def internal_sync_confirm(session_id: str):
    """Step 2: Complete auth after TAN confirmation and run export."""
    if session_id not in _pending_sessions:
        raise HTTPException(status_code=404, detail="Unknown session_id")

    pending = _pending_sessions.pop(session_id)
    client: ComdirectClient = pending["client"]
    config = pending.get("config", {})

    ok = await client.complete_auth(
        pending["session_identifier"],
        pending["challenge_id"],
    )
    if not ok:
        raise HTTPException(status_code=502, detail="Session activation failed")

    # Run export using the authenticated client
    try:
        from pathlib import Path

        from scripts.export_csv import (
            export_account_to_csv,
            export_depot_positions_csv,
            export_depot_transactions_csv,
            export_summary_csv,
        )

        output_dir = Path("/data/exports")
        output_dir.mkdir(parents=True, exist_ok=True)

        data = await client.get_all_data(
            account_transaction_limit=config.get(
                "account_transaction_limit", settings.account_transaction_limit
            ),
            account_transaction_min_booking_date=config.get(
                "account_transaction_min_booking_date",
                settings.account_transaction_min_booking_date,
            ),
            depot_transaction_limit=config.get(
                "depot_transaction_limit", settings.depot_transaction_limit
            ),
            depot_transaction_min_booking_date=config.get(
                "depot_transaction_min_booking_date",
                settings.depot_transaction_min_booking_date,
            ),
        )

        # CSV exports
        for acc in data["accounts"]:
            account_inner = acc.get("account") or acc
            account_id = account_inner.get("accountId")
            iban = account_inner.get("iban", account_id)
            if not account_id:
                continue
            balance_obj = acc.get("balance") or {}
            acc_type_obj = account_inner.get("accountType") or {}
            acc_type_text = (
                acc_type_obj.get("text", "Girokonto")
                if isinstance(acc_type_obj, dict)
                else str(acc_type_obj)
            )
            balance_info = {
                "value": (
                    balance_obj.get("value", "0")
                    if isinstance(balance_obj, dict)
                    else str(balance_obj)
                ),
                "unit": (
                    balance_obj.get("unit", "EUR") if isinstance(balance_obj, dict) else "EUR"
                ),
                "account_type": acc_type_text,
            }
            txs = data["transactions"].get(account_id, [])
            export_account_to_csv(account_id, iban, balance_info, txs, output_dir)

        all_positions: list[dict] = []
        for depot in data["depots"]:
            depot_id = depot.get("depotId")
            if not depot_id:
                continue
            positions = data["depot_positions"].get(depot_id, [])
            all_positions.extend(positions)
            export_depot_positions_csv(depot_id, positions, output_dir)
            depot_txs = data["depot_transactions"].get(depot_id, [])
            export_depot_transactions_csv(depot_id, depot_txs, output_dir)

        export_summary_csv(data["accounts"], all_positions, output_dir)

        # JSON export
        from src.exporter.json_export import build_export

        import json as json_mod

        payload = build_export(data)
        from datetime import date

        json_path = output_dir / f"comdirect_export_{date.today().isoformat()}.json"
        with open(json_path, "w", encoding="utf-8") as fh:
            json_mod.dump(payload, fh, ensure_ascii=False, indent=2)

        logger.info("Export completed successfully")
    except Exception as exc:
        logger.exception("Export failed")
        raise HTTPException(status_code=500, detail=f"Export failed: {exc}")

    return {"status": "done", "message": "Export completed"}
