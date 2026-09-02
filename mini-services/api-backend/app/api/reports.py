"""Scheduled reports API (v48) - cron-driven export jobs.

Endpoints (owner-scoped like the rest of the build surface):
  GET    /reports              list the caller's reports (source names resolved)
  POST   /reports              create one (validates cron + format + source)
  PUT    /reports/{id}         update name/fmt/cron/enabled/source
  DELETE /reports/{id}         drop the report (artifacts stay)
  POST   /reports/{id}/run     run NOW synchronously - returns the artifact id
  GET    /reports/{id}/runs    next fire previews + last-run stats

The generated files are ordinary Artifacts (kind=report) and download via
GET /artifacts/{artifact_id}/content with the caller's normal auth.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user, own_or_404
from ..db import get_db
from ..models import Dashboard, Dataset, ScheduledReport
from ..services import reports as report_svc
from ..services.scheduler import next_fire_times, resync_report_jobs

router = APIRouter(prefix="/reports", tags=["reports"])


class ReportCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    source_type: str = Field(pattern="^(dataset|dashboard)$")
    source_id: str = Field(min_length=1, max_length=36)
    fmt: str = Field(default="csv", max_length=10)
    cron: str = Field(default="0 6 * * *", max_length=100)
    enabled: bool = True


class ReportUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    source_type: str | None = Field(default=None, pattern="^(dataset|dashboard)$")
    source_id: str | None = Field(default=None, min_length=1, max_length=36)
    fmt: str | None = Field(default=None, max_length=10)
    cron: str | None = Field(default=None, max_length=100)
    enabled: bool | None = None


async def _source_name(db: AsyncSession, source_type: str, source_id: str) -> str | None:
    if source_type == "dataset":
        row = await db.get(Dataset, source_id)
        return row.name if row else None
    if source_type == "dashboard":
        row = await db.get(Dashboard, source_id)
        return row.name if row else None
    return None


async def _resolved(db: AsyncSession, rows: list[ScheduledReport]) -> list[dict]:
    out = []
    for r in rows:
        out.append(report_svc.out(r, await _source_name(db, r.source_type, r.source_id)))
    return out


async def _get_or_404(db: AsyncSession, report_id: str, user) -> ScheduledReport:
    row = await db.get(ScheduledReport, report_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Report not found")
    own_or_404(row.owner_id, user)
    return row


async def _validate_body(
    db: AsyncSession,
    *,
    source_type: str,
    source_id: str,
    fmt: str,
    cron: str,
) -> None:
    try:
        report_svc.validate_cron(cron)
        report_svc.validate_format(source_type, fmt)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if source_type == "dataset":
        if await db.get(Dataset, source_id) is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
    else:
        if await db.get(Dashboard, source_id) is None:
            raise HTTPException(status_code=404, detail="Dashboard not found")


@router.get("")
async def list_reports(user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(select(ScheduledReport).order_by(ScheduledReport.created_at.desc()))
    ).scalars().all()
    visible = [r for r in rows if r.owner_id is None or (user and r.owner_id == user.id)]
    return await _resolved(db, visible)


@router.post("", status_code=201)
async def create_report(body: ReportCreate, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    await _validate_body(db, source_type=body.source_type, source_id=body.source_id, fmt=body.fmt, cron=body.cron)
    row = ScheduledReport(
        name=body.name.strip(),
        source_type=body.source_type,
        source_id=body.source_id,
        fmt=body.fmt.strip().lower(),
        cron=body.cron.strip(),
        enabled=body.enabled,
    )
    row.owner_id = user.id if user else None
    db.add(row)
    await db.commit()
    await db.refresh(row)
    await resync_report_jobs(row.id)
    return (await _resolved(db, [row]))[0]


@router.put("/{report_id}")
async def update_report(report_id: str, body: ReportUpdate, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, report_id, user)
    data = body.model_dump(exclude_none=True)
    merged = {
        "name": data.get("name", row.name),
        "source_type": data.get("source_type", row.source_type),
        "source_id": data.get("source_id", row.source_id),
        "fmt": data.get("fmt", row.fmt),
        "cron": data.get("cron", row.cron),
        "enabled": data.get("enabled", row.enabled),
    }
    await _validate_body(
        db,
        source_type=merged["source_type"],
        source_id=merged["source_id"],
        fmt=merged["fmt"],
        cron=merged["cron"],
    )
    row.name = merged["name"].strip()
    row.source_type = merged["source_type"]
    row.source_id = merged["source_id"]
    row.fmt = merged["fmt"].strip().lower()
    row.cron = merged["cron"].strip()
    row.enabled = bool(merged["enabled"])
    db.add(row)
    await db.commit()
    await db.refresh(row)
    await resync_report_jobs(row.id)
    return (await _resolved(db, [row]))[0]


@router.delete("/{report_id}")
async def delete_report(report_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, report_id, user)
    await db.delete(row)
    await db.commit()
    await resync_report_jobs(report_id)  # clears any registered job
    return {"ok": True}


@router.post("/{report_id}/run")
async def run_now(report_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Run the export immediately (synchronous) - the UI's Run now button."""
    row = await _get_or_404(db, report_id, user)
    result = await report_svc.run_report(row.id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Report run failed")
    await db.refresh(row)
    payload = await _resolved(db, [row])
    return {**payload[0], "run": result}


@router.get("/{report_id}/runs")
async def run_preview(report_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, report_id, user)
    return {
        "next_runs": next_fire_times({"mode": "cron", "cron": row.cron}, 5),
        "last_run_at": row.last_run_at,
        "last_status": row.last_status,
        "last_error": row.last_error,
        "fire_count": row.fire_count or 0,
        "last_artifact_id": row.last_artifact_id,
    }
