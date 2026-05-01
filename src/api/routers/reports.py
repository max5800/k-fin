"""Reports router for the Finance API."""

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.api.deps import get_db, require_token
from src.api.schemas import ReportListOut, ReportOut
from src.core.config import settings
from src.core.db.models import Report, ReportStatus

router = APIRouter(
    prefix="/reports",
    tags=["reports"],
    dependencies=[Depends(require_token)],
)

MEDIA_TYPES = {
    "pdf": "application/pdf",
    "md": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8",
}


def _resolve_report_path(report: Report) -> Path:
    """Resolve and validate the report file path against reports_dir."""
    reports_dir = Path(settings.reports_dir).resolve()
    path = (reports_dir / report.file_path).resolve()
    if not path.is_relative_to(reports_dir):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid report path",
        )
    if not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Report file not found on disk",
        )
    return path


@router.get("", response_model=ReportListOut)
def list_reports(
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    format: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    report_type: str | None = Query(None),
):
    stmt = select(Report)
    count_stmt = select(func.count()).select_from(Report)

    if format:
        stmt = stmt.where(Report.format == format)
        count_stmt = count_stmt.where(Report.format == format)
    if status_filter:
        stmt = stmt.where(Report.status == status_filter)
        count_stmt = count_stmt.where(Report.status == status_filter)
    if report_type:
        stmt = stmt.where(Report.report_type == report_type)
        count_stmt = count_stmt.where(Report.report_type == report_type)

    total = db.execute(count_stmt).scalar_one()

    stmt = (
        stmt.order_by(Report.period_start.desc(), Report.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = db.execute(stmt).scalars().all()

    return ReportListOut(
        items=[ReportOut.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{report_id}", response_model=ReportOut)
def get_report(
    report_id: str,
    db: Session = Depends(get_db),
):
    report = db.get(Report, report_id)
    if not report:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    return ReportOut.model_validate(report)


@router.get("/{report_id}/download")
def download_report(
    report_id: str,
    db: Session = Depends(get_db),
):
    report = db.get(Report, report_id)
    if not report:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    if report.status != ReportStatus.READY:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Report is not ready (status: {report.status.value})",
        )

    # Agent-produced JSON reports live entirely in `Report.content` — there
    # is no file on disk. Serve them directly so the download button works
    # without a parallel file-export pipeline.
    if report.file_path is None and report.content is not None:
        body = json.dumps(report.content, ensure_ascii=False, indent=2).encode("utf-8")
        filename = f"{report.report_type}-{report.period_start.isoformat()}.json"
        return Response(
            content=body,
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    if report.file_path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Report has no downloadable payload",
        )

    path = _resolve_report_path(report)
    media_type = MEDIA_TYPES.get(report.format, "application/octet-stream")

    return FileResponse(
        path,
        media_type=media_type,
        filename=path.name,
    )
