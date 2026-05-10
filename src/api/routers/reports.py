"""Reports router for the Finance API."""

import json

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.api.deps import get_db, require_token
from src.api.schemas import ReportListOut, ReportOut
from src.core.db.models import Report, ReportStatus

router = APIRouter(
    prefix="/reports",
    tags=["reports"],
    dependencies=[Depends(require_token)],
)


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

    # Agent-produced JSON reports live entirely in `Report.content`.
    if report.content is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Report has no downloadable payload",
        )

    body = json.dumps(report.content, ensure_ascii=False, indent=2).encode("utf-8")
    filename = f"{report.report_type}-{report.period_start.isoformat()}.json"
    return Response(
        content=body,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
