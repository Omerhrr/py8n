"""Platform overview API (v67) - one derived answer to the vision sentence.

"A platform for composing, building, training, deploying and operating
data, AI and software systems."  GET /platform reads the five verbs off
the live tables (cheap counts, nothing stored):

* COMPOSING  - Py8n Systems + Model Systems (the operating units)
* BUILDING   - builder drafts + marketplace installs (how new systems arrive)
* TRAINING   - registry versions, the language share, versions added in 7d
* DEPLOYING  - deployments, live ones, serving invocations in 7d
* OPERATING  - executions + failure rate, datasets, dashboards, reports

``verdicts`` states per verb whether the estate shows real usage, and
``ready`` is the honest overall answer: every verb has evidence. The
counts are owner-scoped like every other surface.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user
from ..db import get_db
from ..models import (
    Dashboard,
    Dataset,
    ExecutionLog,
    ModelDeployment,
    ModelSystem,
    ScheduledReport,
    Solution,
    SystemDraft,
    TrainedModel,
    Workflow,
    Py8nSystem,
)
from ..services.deployments import LM_ALGORITHMS

router = APIRouter(prefix="/platform", tags=["platform"])


def _as_utc(dt: datetime) -> datetime:
    """SQLite returns naive datetimes - treat them as UTC (they are)."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


@router.get("")
async def platform_overview(user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    owner = getattr(user, "id", None)
    since = datetime.now(timezone.utc) - timedelta(days=7)

    def _own(rows: list) -> list:
        if owner is None:
            return rows
        return [r for r in rows if getattr(r, "owner_id", None) in (owner, None)]

    # ---- composing -------------------------------------------------------
    systems = _own((await db.execute(select(Py8nSystem))).scalars().all())
    model_systems = _own((await db.execute(select(ModelSystem))).scalars().all())
    composing = {
        "systems": len(systems),
        "model_systems": len(model_systems),
    }

    # ---- building --------------------------------------------------------
    drafts = _own((await db.execute(select(SystemDraft))).scalars().all())
    solutions = (await db.execute(select(Solution))).scalars().all()
    building = {
        "drafts": len(drafts),
        "built": sum(1 for d in drafts if d.status == "built"),
        "solutions": len(solutions),
        "installs": sum(s.installs or 0 for s in solutions),
    }

    # ---- training --------------------------------------------------------
    models = _own((await db.execute(select(TrainedModel))).scalars().all())
    training = {
        "registry_versions": len(models),
        "active_models": len({m.name for m in models if m.active}),
        "language_versions": sum(1 for m in models if m.algorithm in LM_ALGORITHMS),
        "versions_7d": sum(1 for m in models if m.created_at and _as_utc(m.created_at) >= since),
    }

    # ---- deploying -------------------------------------------------------
    deployments = _own((await db.execute(select(ModelDeployment))).scalars().all())
    dep_wf_ids = [d.workflow_id for d in deployments if d.workflow_id]
    invocations_7d = 0
    if dep_wf_ids:
        runs = (await db.execute(
            select(ExecutionLog).where(
                ExecutionLog.workflow_id.in_(dep_wf_ids),  # type: ignore[attr-defined]
                ExecutionLog.started_at >= since)))
        invocations_7d = len(runs.scalars().all())
    deploying = {
        "deployments": len(deployments),
        "live": sum(1 for d in deployments if d.enabled),
        "serving_invocations_7d": invocations_7d,
    }

    # ---- operating -------------------------------------------------------
    workflows = _own((await db.execute(select(Workflow))).scalars().all())
    runs_7d = (await db.execute(
        select(ExecutionLog).where(ExecutionLog.started_at >= since))).scalars().all()
    runs_7d = [r for r in runs_7d if owner is None or _row_owned(r.workflow_id, workflows, owner)]
    failures_7d = sum(1 for r in runs_7d if r.status == "error")
    datasets = _own((await db.execute(select(Dataset))).scalars().all())
    dashboards = _own((await db.execute(select(Dashboard))).scalars().all())
    reports = _own((await db.execute(select(ScheduledReport))).scalars().all())
    operating = {
        "workflows": len(workflows),
        "datasets": len(datasets),
        "dashboards": len(dashboards),
        "scheduled_reports": len(reports),
        "executions_7d": len(runs_7d),
        "failures_7d": failures_7d,
        "failure_rate_7d": round(failures_7d / len(runs_7d) * 100, 1) if runs_7d else 0.0,
    }

    verdicts = {
        "composing": composing["systems"] > 0 or composing["model_systems"] > 0,
        "building": building["built"] > 0 or building["installs"] > 0,
        "training": training["registry_versions"] > 0,
        "deploying": any(d.enabled for d in deployments) and invocations_7d > 0,
        "operating": operating["executions_7d"] > 0 and (
            operating["dashboards"] > 0 or operating["scheduled_reports"] > 0
            or operating["datasets"] > 0),
    }

    return {
        "vision": "A platform for composing, building, training, deploying and operating "
                  "data, AI and software systems.",
        "composing": composing,
        "building": building,
        "training": training,
        "deploying": deploying,
        "operating": operating,
        "verdicts": verdicts,
        "ready": all(verdicts.values()),
    }


def _row_owned(workflow_id: str, workflows: list, owner: str) -> bool:
    for wf in workflows:
        if wf.id == workflow_id:
            return wf.owner_id in (owner, None)
    return False
