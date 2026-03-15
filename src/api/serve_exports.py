"""
Read-only REST API to serve exported CSV files.

Designed to run in an isolated container with access only to the
exports volume — no Comdirect credentials, no source code access.
"""

import os
import re
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

EXPORTS_DIR = Path(os.getenv("EXPORTS_DIR", "/data/exports"))
API_TOKEN = os.getenv("API_TOKEN", "")

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


def _check_token(token: str) -> None:
    if API_TOKEN and token != API_TOKEN:
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


@app.get("/exports")
def list_exports(token: str = ""):
    """List all available CSV export files."""
    _check_token(token)
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
def latest_exports(token: str = ""):
    """Get the most recent file of each export type."""
    _check_token(token)
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
def download_export(filename: str, token: str = ""):
    """Download a specific CSV export file."""
    _check_token(token)
    path = _safe_filename(filename)
    return FileResponse(
        path,
        media_type="text/csv; charset=utf-8-sig",
        filename=filename,
    )
