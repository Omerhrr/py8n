"""Scheduled report exports (v48) - cron-driven snapshots of the data estate.

A ScheduledReport points at one source (a dataset or a dashboard) plus an
export format and a crontab. When APScheduler fires the job, the service
serializes the source through the SAME code paths the download endpoints
use (``datasets.export_dataset_bytes`` / ``dashboards.compute_config``) and
stores the bytes as a regular Artifact. The report row remembers the last
artifact so the Reports page can deep-link the freshest download.

Design contracts:

- The job callback NEVER raises: a broken source or a failed write only
  lands on the report row (last_status/last_error) - a bad export can
  never take the scheduler down.
- Owner-scoped: rows are listed/created for the caller; the scheduler job
  runs system-side and stamps artifact meta with the report identity.
- Cron-only: report exports are time-of-day concerns; one crontab string
  validates and previews with a single code path (APScheduler CronTrigger).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Dataset, ScheduledReport

logger = logging.getLogger("py8n.reports")

DATASET_FORMATS = ("csv", "xlsx", "json", "parquet")
# v49: dashboards grow "png" - a server-rendered image of every component
# (services/report_images.py), produced from the SAME compute_config output
# as the JSON snapshot.
DASHBOARD_FORMATS = ("json", "png")


def validate_cron(cron: str) -> str:
    """Raise ValueError when the crontab is malformed."""
    cleaned = (cron or "").strip()
    if not cleaned:
        raise ValueError("A cron expression is required (e.g. '0 6 * * *')")
    try:
        CronTrigger.from_crontab(cleaned, timezone="UTC")
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid cron expression {cleaned!r}: {exc}") from exc
    return cleaned


def validate_format(source_type: str, fmt: str) -> str:
    cleaned = (fmt or "").strip().lower()
    allowed = DATASET_FORMATS if source_type == "dataset" else DASHBOARD_FORMATS
    if cleaned not in allowed:
        raise ValueError(
            f"Format {fmt!r} is not valid for a {source_type} report (allowed: {', '.join(allowed)})"
        )
    return cleaned


def out(row: ScheduledReport, source_name: str | None = None) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "source_type": row.source_type,
        "source_id": row.source_id,
        "source_name": source_name,
        "fmt": row.fmt,
        "cron": row.cron,
        "enabled": bool(row.enabled),
        "created_at": row.created_at,
        "last_run_at": row.last_run_at,
        "fire_count": row.fire_count or 0,
        "last_status": row.last_status,
        "last_error": row.last_error,
        "last_artifact_id": row.last_artifact_id,
    }


async def build_report_bytes(
    db: AsyncSession,
    *,
    source_type: str,
    source_id: str,
    fmt: str,
) -> tuple[bytes, str, str, str]:
    """Serialize the source -> (bytes, content_type, ext, suggested_name).

    Raises ValueError (bad format), LookupError (source missing) - callers
    translate for their surface.
    """
    if source_type == "dataset":
        from ..services import datasets as ds_svc

        ds = await db.get(Dataset, source_id)
        if ds is None:
            raise LookupError("Dataset not found")
        data, content_type, ext = ds_svc.export_dataset_bytes(ds, fmt)
        safe_name = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in ds.name)
        return data, content_type, ext, f"{safe_name or 'dataset'}.{ext}"

    if source_type == "dashboard":
        from ..models import Dashboard
        from ..services import dashboards as dash_svc
        from ..services import datasets as ds_svc

        board = await db.get(Dashboard, source_id)
        if board is None:
            raise LookupError("Dashboard not found")
        components = (board.config or {}).get("components", [])
        # dedupe dataset ids, preload each referenced frame once
        ids: list[str] = []
        for comp in components:
            dsid = comp.get("dataset_id")
            if dsid and dsid not in ids:
                ids.append(dsid)
        loaders: dict[str, "object"] = {}
        names: dict[str, str] = {}
        for dsid in ids:
            ds = await db.get(Dataset, dsid)
            if ds is not None:
                loaders[dsid] = ds_svc.read_parquet_df(ds_svc.parquet_path(ds.id))
                names[dsid] = ds.name
        rendered = dash_svc.compute_config(components, loaders)
        generated_at = datetime.now(timezone.utc)
        if fmt == "png":  # v49: image snapshot of the rendered board
            from ..services.report_images import render_dashboard_png

            return (
                render_dashboard_png(board.name, rendered, generated_at=generated_at),
                "image/png",
                "png",
                f"{board.slug or board.name}.report.png",
            )
        import json as _json

        payload = {
            "dashboard": {"name": board.name, "slug": board.slug},
            "datasets": names,
            "generated_at": generated_at.isoformat(),
            "filters": {},
            "components": rendered,
        }
        data = _json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        return data, "application/json", "json", f"{board.slug or board.name}.report.json"

    raise ValueError(f"Unknown report source_type {source_type!r}")


async def run_report(report_id: str) -> dict:
    """Execute one report NOW (also the APScheduler callback).

    Opens its own session end to end; every failure path lands on the row
    as last_status=error and the function still returns a summary instead
    of raising, so a broken report can never wedge the scheduler.
    """
    from ..db import AsyncSessionLocal
    from ..services import artifacts as artifact_svc

    async with AsyncSessionLocal() as session:
        row = await session.get(ScheduledReport, report_id)
        if row is None or not row.enabled:
            return {"ok": False, "error": "Report not found or disabled"}

        source_name: str | None = None
        try:
            data, content_type, ext, filename = await build_report_bytes(
                session,
                source_type=row.source_type,
                source_id=row.source_id,
                fmt=row.fmt,
            )
            if row.source_type == "dataset":
                src = await session.get(Dataset, row.source_id)
                source_name = src.name if src else None
            else:
                from ..models import Dashboard

                src = await session.get(Dashboard, row.source_id)
                source_name = src.name if src else None

            artifact = await artifact_svc.save_artifact(
                session,
                kind="report",
                data=data,
                content_type=content_type,
                meta={
                    "title": f"{row.name} ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')})",
                    "report_id": row.id,
                    "report_name": row.name,
                    "source_type": row.source_type,
                    "source_id": row.source_id,
                    "source_name": source_name,
                    "fmt": row.fmt,
                },
                filename=filename,
            )
            row.last_artifact_id = artifact.id
            row.last_status = "ok"
            row.last_error = None
            ok = True
            error = None
        except Exception as exc:  # noqa: BLE001 - the job must never raise
            ok = False
            error = f"{exc.__class__.__name__}: {exc}"[:290]
            logger.warning("scheduled report %s failed: %s", report_id, error)
            row.last_status = "error"
            row.last_error = error

        row.last_run_at = datetime.now(timezone.utc)
        row.fire_count = (row.fire_count or 0) + 1
        await session.commit()
        return {
            "ok": ok,
            "error": error,
            "artifact_id": row.last_artifact_id if ok else None,
            "ran_at": row.last_run_at.isoformat() if row.last_run_at else None,
        }
