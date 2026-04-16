"""
Read-only REST API to serve exported CSV files and trigger syncs.

Public-facing service (comdirect-api). Has NO bank secrets — delegates
sync work to the internal comdirect-worker via HTTP.
"""

import hmac
import os
import re
from collections import defaultdict
from pathlib import Path

import httpx
from fastapi import Body, Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, field_validator

EXPORTS_DIR = Path(os.getenv("EXPORTS_DIR", "/data/exports"))
API_TOKEN = os.getenv("API_TOKEN", "")
if not API_TOKEN:
    raise RuntimeError("CRITICAL: API_TOKEN environment variable is required")
WORKER_URL = os.getenv("WORKER_URL", "http://comdirect-worker:8001")

app = FastAPI(
    title="Comdirect Finance Export API",
    description="Read-only API for exported financial CSV and JSON data.",
    version="1.0.0",
)

_cors_origins = [
    o.strip()
    for o in os.getenv(
        "CORS_ORIGINS", "http://localhost:3000,http://localhost:8080"
    ).split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)

FILE_PATTERN = re.compile(r"^[\w\-]+\.(csv|json)$")

# File type prefixes for grouping
FILE_TYPES = {
    "umsaetze": "Konto-Umsätze",
    "depot_positionen": "Depot-Positionen",
    "depot_umsaetze": "Depot-Transaktionen",
    "finanzuebersicht": "Finanzübersicht",
    "comdirect_export": "JSON-Gesamtexport",
}

_bearer_scheme = HTTPBearer(auto_error=False)


class SyncStartRequest(BaseModel):
    account_transaction_limit: int | None = None
    account_transaction_min_booking_date: str | None = None
    depot_transaction_limit: int | None = None
    depot_transaction_min_booking_date: str | None = None

    @field_validator(
        "account_transaction_min_booking_date",
        "depot_transaction_min_booking_date",
    )
    @classmethod
    def validate_relative_date(cls, v: str | None) -> str | None:
        if v is None:
            return v
        m = re.match(r"^-(\d+)d$", v)
        if m and int(m.group(1)) > 3650:
            raise ValueError("Relative date must not exceed 3650 days (~10 years)")
        return v


def _check_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> None:
    if not credentials or not hmac.compare_digest(credentials.credentials, API_TOKEN):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _safe_filename(filename: str) -> Path:
    """Validate filename to prevent path traversal."""
    if not FILE_PATTERN.match(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = (EXPORTS_DIR / filename).resolve()
    if not path.is_relative_to(EXPORTS_DIR.resolve()):
        raise HTTPException(status_code=400, detail="Invalid path")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return path


@app.get("/health")
def health():
    return {"status": "ok", "service": "comdirect-export-api"}


@app.get("/exports")
def list_exports(_auth: None = Depends(_check_token)):
    """List all available CSV export files."""
    if not EXPORTS_DIR.is_dir():
        return {"files": []}

    files = []
    for f in sorted(
        [p for p in EXPORTS_DIR.glob("*.*") if p.suffix in (".csv", ".json")],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        files.append(
            {
                "filename": f.name,
                "size_bytes": f.stat().st_size,
                "modified": f.stat().st_mtime,
            }
        )
    return {"files": files}


@app.get("/exports/latest")
def latest_exports(_auth: None = Depends(_check_token)):
    """Get the most recent file of each export type."""
    if not EXPORTS_DIR.is_dir():
        return {"latest": {}}

    grouped: dict[str, list[Path]] = defaultdict(list)
    for f in EXPORTS_DIR.glob("*.*"):
        if f.suffix not in (".csv", ".json"):
            continue
        for prefix in FILE_TYPES:
            if f.name.startswith(prefix):
                grouped[prefix].append(f)
                break

    latest = {}
    for prefix, paths in grouped.items():
        newest = max(paths, key=lambda p: p.stat().st_mtime)
        latest[prefix] = {
            "label": FILE_TYPES[prefix],
            "filename": newest.name,
            "size_bytes": newest.stat().st_size,
            "modified": newest.stat().st_mtime,
        }
    return {"latest": latest}


@app.get("/exports/{filename}")
def download_export(filename: str, _auth: None = Depends(_check_token)):
    """Download a specific CSV export file."""
    path = _safe_filename(filename)
    media_type = (
        "application/json" if path.suffix == ".json" else "text/csv; charset=utf-8-sig"
    )
    return FileResponse(
        path,
        media_type=media_type,
        filename=filename,
    )


@app.post("/sync/start")
async def sync_start(
    payload: SyncStartRequest | None = Body(default=None),
    _auth: None = Depends(_check_token),
):
    """Begin a sync run — triggers TAN challenge via the internal worker.

    Optional request fields override the worker defaults for this sync only.
    """
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{WORKER_URL}/internal/sync/start",
                json=(payload.model_dump(exclude_none=True) if payload else None),
            )
            return resp.json()
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="Worker service unreachable")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=503, detail=f"Worker communication failed: {e}")


@app.post("/sync/confirm")
async def sync_confirm(session_id: str, _auth: None = Depends(_check_token)):
    """Confirm TAN and complete sync via the internal worker."""
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{WORKER_URL}/internal/sync/confirm",
                params={"session_id": session_id},
            )
            return resp.json()
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="Worker service unreachable")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=503, detail=f"Worker communication failed: {e}")
