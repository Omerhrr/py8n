"""Global schedules overview (v7) - every schedule trigger across workflows.

GET /api/v1/schedules returns one row per schedule_trigger node, ordered so
that active schedules with a known next fire time come first (soonest first),
then broken/invalid ones, then paused workflows.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user, scope_rows
from ..db import get_db
from ..models import Workflow
from ..schemas import GlobalScheduleEntryOut
from ..services.scheduler import schedule_entries_for_graph

router = APIRouter(prefix="/schedules", tags=["schedules"])

_FAR_FUTURE = "9999-12-31T23:59:59+00:00"


@router.get("", response_model=list[GlobalScheduleEntryOut])
async def list_schedules(user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Workflow).order_by(Workflow.name))).scalars().all()
    rows = scope_rows(rows, user)  # v37
    entries: list[GlobalScheduleEntryOut] = []
    for wf in rows:
        for entry in schedule_entries_for_graph(wf.graph or {}):
            entries.append(
                GlobalScheduleEntryOut(
                    **entry,
                    workflow_id=wf.id,
                    workflow_name=wf.name,
                    is_active=wf.is_active,
                )
            )

    def _sort_key(e: GlobalScheduleEntryOut):
        healthy = bool(e.next_runs) and e.error is None
        next_run = e.next_runs[0] if e.next_runs else _FAR_FUTURE
        if not e.is_active:
            group = 2  # paused workflows last
        elif not healthy:
            group = 1  # invalid schedule params
        else:
            group = 0  # active + healthy, soonest first
        return (group, next_run, e.workflow_name)

    entries.sort(key=_sort_key)
    return entries
