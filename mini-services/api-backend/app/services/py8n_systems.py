"""Py8n Systems (v61) - workflows, datasets, apps, dashboards, models and
reports bound into ONE operating unit.

The roadmap's endpoint of the platform arc: users stop creating
workflows and start creating systems. A system is a curated grouping
(membership is stored, like folders), but everything it REPORTS is
derived at read time - health, activity and delivery outcomes come from
the member objects themselves, so a system summary can never drift from
reality.

Attach validation resolves every reference against the live table with
owner scoping, so a system can never hold a foreign or nonexistent
object. The health rollup reuses the same primitives the Operations
Center uses (v50 dataset health, v53 pipeline rollups, v52 delivery
outcomes) scoped to the member ids.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    App,
    Dashboard,
    Dataset,
    ExecutionLog,
    ModelSystem,
    Py8nSystem,
    ReportDeliveryEvent,
    ScheduledReport,
    SystemComponent,
    TrainedModel,
    Workflow,
)
from .health import compute_health

COMPONENT_KINDS = ("workflow", "dataset", "app", "dashboard", "model", "report", "model_system")
KIND_TABLES = {
    "workflow": Workflow,
    "dataset": Dataset,
    "app": App,
    "dashboard": Dashboard,
    "model": TrainedModel,
    "report": ScheduledReport,
    "model_system": ModelSystem,  # v63: the model-building operating unit
}
HEALTH_BUDGET = 10  # datasets fully health-scored per system-health call


async def resolve_component(db: AsyncSession, kind: str, ref_id: str, user_id: str | None):
    """Resolve a (kind, ref_id) against the live table + owner scope.

    Returns the row, or raises ValueError with a user-safe message.
    """
    if kind not in COMPONENT_KINDS:
        raise ValueError(f"unknown component kind {kind!r} (allowed: {', '.join(COMPONENT_KINDS)})")
    model = KIND_TABLES[kind]
    row = await db.get(model, ref_id)
    if row is None:
        raise ValueError(f"{kind} {ref_id} not found")
    owner = getattr(row, "owner_id", None)
    if user_id and owner not in (user_id, None):
        raise ValueError(f"{kind} {ref_id} not found")  # foreign rows look nonexistent
    return row


def _slug_counts(components: list[SystemComponent]) -> dict:
    counts = {k: 0 for k in COMPONENT_KINDS}
    for c in components:
        counts[c.kind] = counts.get(c.kind, 0) + 1
    return counts


async def system_health(db: AsyncSession, system: Py8nSystem) -> dict:
    """Derived health for one system - nothing stored.

    Workflows: runs/failures over 7d + failure rate (member-scoped).
    Datasets: v50 health tiers (budget-capped profiling).
    Reports: delivery outcomes over 7d.
    Verdict: healthy / degraded / unhealthy, mirroring the ops center.
    """
    comps = list(system.components or [])
    by_kind = _slug_counts(comps)

    now = datetime.now(timezone.utc)
    d7 = now - timedelta(days=7)

    wf_ids = [c.ref_id for c in comps if c.kind == "workflow"]
    ds_ids = [c.ref_id for c in comps if c.kind == "dataset"]
    report_ids = [c.ref_id for c in comps if c.kind == "report"]
    model_system_ids = [c.ref_id for c in comps if c.kind == "model_system"]  # v63

    runs_7d = failures_7d = 0
    last_error = None
    failing: list[dict] = {}
    if wf_ids:
        rows = (
            (
                await db.execute(
                    select(ExecutionLog)
                    .where(ExecutionLog.workflow_id.in_(wf_ids), ExecutionLog.started_at >= d7)
                    .order_by(ExecutionLog.started_at.desc())
                    .limit(500)
                )
            )
            .scalars()
            .all()
        )
        runs_7d = len(rows)
        for ex in rows:
            if ex.status == "error":
                failures_7d += 1
                entry = failing.setdefault(ex.workflow_id, {"failures": 0, "last_error": None})
                entry["failures"] += 1
                if entry["last_error"] is None:
                    entry["last_error"] = (ex.error or "")[:200] or None
                if last_error is None:
                    last_error = (ex.error or "")[:200] or None
        wf_names = dict(
            (await db.execute(select(Workflow.id, Workflow.name).where(Workflow.id.in_(wf_ids)))).all()
        )
        failing_workflows = [
            {"workflow_id": wid, "name": wf_names.get(wid, wid[:8]), **entry}
            for wid, entry in failing.items()
        ]
        failing_workflows.sort(key=lambda x: x["failures"], reverse=True)
    else:
        failing_workflows = []

    datasets = {"total": len(ds_ids), "healthy": 0, "degraded": 0, "unhealthy": 0, "unscored": 0, "worst": None}
    if ds_ids:
        rows = (await db.execute(select(Dataset).where(Dataset.id.in_(ds_ids)))).scalars().all()
        for i, ds in enumerate(rows):
            if i >= HEALTH_BUDGET:
                datasets["unscored"] += 1
                continue
            try:
                h = await compute_health(db, ds)
            except Exception:
                datasets["unscored"] += 1
                continue
            datasets[h["status"]] = datasets.get(h["status"], 0) + 1
            if datasets["worst"] is None or h["score"] < datasets["worst"]["score"]:
                datasets["worst"] = {"dataset_id": ds.id, "name": ds.name, "score": h["score"], "status": h["status"]}

    deliveries = {"ok_7d": 0, "error_7d": 0}
    if report_ids:
        ev_rows = (
            await db.execute(
                select(ReportDeliveryEvent)
                .where(ReportDeliveryEvent.report_id.in_(report_ids), ReportDeliveryEvent.created_at >= d7)
            )
        ).scalars().all()
        for ev in ev_rows:
            if ev.status in ("ok", "error"):
                deliveries[f"{ev.status}_7d"] += 1

    failure_rate = round(failures_7d / runs_7d * 100, 1) if runs_7d else 0.0
    verdict = "healthy"
    if failures_7d or datasets["unhealthy"] or deliveries["error_7d"] or datasets["degraded"]:
        verdict = "degraded"
    if (runs_7d >= 5 and failure_rate >= 50) or (datasets["unhealthy"] and datasets["unhealthy"] >= datasets["healthy"] and datasets["healthy"] == 0):
        verdict = "unhealthy"

    return {
        "verdict": verdict,
        "workflows": {"bound": len(wf_ids), "runs_7d": runs_7d, "failures_7d": failures_7d,
                      "failure_rate_7d": failure_rate, "failing_workflows": failing_workflows[:5]},
        "datasets": datasets,
        "reports": {"bound": len(report_ids), **deliveries},
        "model_systems": {"bound": len(model_system_ids)},
        "generated_at": now.isoformat(),
    }


def system_summary(db_rows: Py8nSystem) -> dict:
    comps = list(db_rows.components or [])
    return {
        "id": db_rows.id,
        "name": db_rows.name,
        "description": db_rows.description,
        "icon": db_rows.icon,
        "color": db_rows.color,
        "components": _slug_counts(comps),
        "total_components": len(comps),
        "created_at": db_rows.created_at.isoformat() if db_rows.created_at else None,
    }
