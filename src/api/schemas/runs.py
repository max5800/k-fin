"""Pydantic schemas for run endpoints."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class RunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    source: str
    agent_type: str | None = None
    status: str
    started_at: datetime
    finished_at: datetime | None
    rows_processed: int
    error: str | None


class IngestRequest(BaseModel):
    """Request body for manual re-ingest from an export file."""

    filename: str
