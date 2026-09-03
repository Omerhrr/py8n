"""The observability API (v53) - one surface for "is the estate okay?".

* ``GET /observability/overview`` - fleet-wide health: dataset health
  tiers (budget-capped profiling), pipeline run/failure rates over 7d,
  incremental-ingestion checkpoints, report-delivery outcomes, and the
  newest error-severity incidents from the unified stream.
* ``GET /observability/events`` - the unified event stream, stitched
  from the domain tables (dataset writes, workflow outcomes, report
  deliveries, denied share attempts). Derived, never stored.

Both endpoints are owner-scoped exactly like the estate itself: a
signed-in caller sees their own rows plus unclaimed ones.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user
from ..db import get_db
from ..services import observability as obs_svc

router = APIRouter(prefix="/observability", tags=["observability"])


@router.get("/overview")
async def observability_overview(
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """Fleet-wide snapshot: dataset health, pipeline reliability, ingestion,
    deliveries and current incidents."""
    user_id = getattr(user, "id", None)
    return await obs_svc.build_overview(db, user_id)


@router.get("/events")
async def observability_events(
    type: str = Query(default="", max_length=400, description="Comma-separated type filter (prefix match, e.g. 'workflow.,report.delivery_failed')"),
    severity: str = Query(default="", pattern="^(|info|warn|error)$", description="Filter by severity"),
    hours: float = Query(default=168.0, gt=0, le=8760, description="How far back to look (default 7d)"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """The unified event stream (derived from the domain tables)."""
    user_id = getattr(user, "id", None)
    types = [t for t in type.split(",") if t.strip()] if type else None
    return await obs_svc.list_events(
        db,
        user_id,
        types=types,
        severity=severity or None,
        hours=hours,
        limit=limit,
        offset=offset,
    )
