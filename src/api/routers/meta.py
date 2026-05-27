"""Small app metadata endpoints for the authenticated UI shell."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from src.api.deps import Auth
from src.core.version import backend_version

router = APIRouter(prefix="/meta", tags=["meta"], dependencies=[Auth])


class VersionOut(BaseModel):
    backend_version: str


@router.get("/version", response_model=VersionOut)
def version():
    return VersionOut(backend_version=backend_version())
