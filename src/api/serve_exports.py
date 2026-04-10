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
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

EXPORTS_DIR = Path(os.getenv("EXPORTS_DIR", "/data/exports"))
API_TOKEN = os.getenv("API_TOKEN", "")
WORKER_URL = os.getenv("WORKER_URL", "http://comdirect-worker:8001")

app = FastAPI(
    title="Comdirect Finance Export API",
    description="Read-only API for exported financial CSV data.",
    version="1.0.0",
)

CSV_PATTERN = re.compile(r"^[\w\-]+\.csv$")

# File type prefixes for grouping
FILE_TYPES = {
    "umsaetze": "Konto-Umsätze",
    "depot_positionen": "Depot-Positionen",
    "depot_umsaetze": "Depot-Transaktionen",
    "finanzuebersicht": "Finanzübersicht",
}

_bearer_scheme = HTTPBearer(auto_error=False)


def _check_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> None:
    if not API_TOKEN:
        return
    if not credentials or not hmac.compare_digest(credentials.credentials, API_TOKEN):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _safe_filename(filename: str) -> Path:
    """Validate filename to prevent path traversal."""
    if not CSV_PATTERN.match(filename):
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
        EXPORTS_DIR.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True
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
    for f in EXPORTS_DIR.glob("*.csv"):
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
    return FileResponse(
        path,
        media_type="text/csv; charset=utf-8-sig",
        filename=filename,
    )


@app.post("/sync/start")
async def sync_start(_auth: None = Depends(_check_token)):
    """Begin a sync run — triggers TAN challenge via the internal worker."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{WORKER_URL}/internal/sync/start")
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
