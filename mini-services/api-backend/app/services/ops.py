"""Automation Operations Center (v57) - "is my ENTIRE environment healthy?".

v53 observability answered "are my datasets healthy?" and stitched a
per-surface event stream. The ops center is the control plane on top:

**ops_overview** composes the whole-environment rollup the roadmap asks
for - SYSTEM verdict, workflows (total/active/running now/failed 24h),
datasets (health tiers via the v53 budgeted primitive), reports
(scheduled + delivery failures), agents (workflows carrying agent/LLM
nodes and their 7d error record) and the open incidents (72h, with
execution ids so the UI can drill down).

**incident_chain** is the drilldown the roadmap draws as
``Incident -> Workflow -> Execution -> Node -> Input -> Error -> Related
dataset -> Impact``: one derived response that walks a failed execution
through its workflow, the node(s) that killed it (with the input they
received), the error itself, a comparison against the previous successful
run, the datasets the workflow touches (with live health), and the v55
impact report for what breaks if those datasets change.

Nothing is stored, so the control plane cannot drift from reality.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    Dashboard,
    Dataset,
    ExecutionLog,
    ScheduledReport,
    Workflow,
)
from .health import compute_health
from .impact import _graph_mentions, _view_names, compute_impact
from .observability import (
    _aware,
    _dataset_overview,
    _delivery_overview,
    _iso,
    _pipeline_overview,
    list_events,
)

HEALTH_BUDGET = 3      # datasets fully health-scored per incident call
IMPACT_BUDGET = 2      # datasets impact-scored per incident call
AGENT_NODE_TYPES = {"agent", "llm_chat"}
_DATA_NODE_TYPES = {"dataset_read", "dataset_write", "sql_query", "dataset_export"}


async def _running_now(db: AsyncSession, user_id: str) -> int:
    q = select(ExecutionLog).where(ExecutionLog.status == "running")
    rows = (await db.execute(q.order_by(ExecutionLog.started_at.desc()).limit(200))).scalars().all()
    if not user_id:
        return len(rows)
    wf_ids = {ex.workflow_id for ex in rows}
    if not wf_ids:
        return 0
    owned = (
        await db.execute(select(Workflow.id).where(Workflow.id.in_(wf_ids), Workflow.owner_id.in_([user_id, None])))
    ).scalars().all()
    allowed = set(owned)
    return sum(1 for ex in rows if ex.workflow_id in allowed)


async def _agents_overview(db: AsyncSession, user_id: str) -> dict:
    """Workflows that carry agent / LLM nodes - the AI surface of the estate."""
    wfs = (
        (await db.execute(select(Workflow).where(Workflow.owner_id.in_([user_id, None])))).scalars().all()
        if user_id else (await db.execute(select(Workflow))).scalars().all()
    )
    agent_wfs = [
        w for w in wfs
        if any(n.get("type") in AGENT_NODE_TYPES for n in (w.graph or {}).get("nodes", []))
    ]
    ids = [w.id for w in agent_wfs]
    runs_7d = 0
    errors_7d = 0
    last_error = None
    if ids:
        d7 = datetime.now(timezone.utc) - timedelta(days=7)
        rows = (
            await db.execute(
                select(ExecutionLog)
                .where(ExecutionLog.workflow_id.in_(ids), ExecutionLog.started_at >= d7)
                .order_by(ExecutionLog.started_at.desc())
                .limit(500)
            )
        ).scalars().all()
        runs_7d = len(rows)
        for ex in rows:
            if ex.status == "error":
                errors_7d += 1
                if last_error is None:
                    last_error = (ex.error or "")[:200] or None
    return {
        "agent_workflows": len(agent_wfs),
        "runs_7d": runs_7d,
        "errors_7d": errors_7d,
        "last_error": last_error,
        "workflows": [{"id": w.id, "name": w.name, "ref": f"/workflows/{w.id}"} for w in agent_wfs[:8]],
    }


async def ops_overview(db: AsyncSession, user_id: str) -> dict:
    """The whole-environment rollup - workflows, datasets, reports, agents."""
    ds = await _dataset_overview(db, user_id)
    pipes = await _pipeline_overview(db, user_id)
    deliveries = await _delivery_overview(db, user_id)
    agents = await _agents_overview(db, user_id)
    running = await _running_now(db, user_id)

    reports_total = (
        len((await db.execute(select(ScheduledReport).where(ScheduledReport.owner_id.in_([user_id, None])))).scalars().all())
        if user_id else len((await db.execute(select(ScheduledReport))).scalars().all())
    )
    dashboards_total = (
        len((await db.execute(select(Dashboard).where(Dashboard.owner_id.in_([user_id, None])))).scalars().all())
        if user_id else len((await db.execute(select(Dashboard))).scalars().all())
    )

    stream = await list_events(db, user_id, severity="error", hours=72, limit=12, offset=0)
    incidents = list(stream["events"])  # already severity=error, newest first

    # The pipeline rollup only gives 7d numbers - count 24h directly
    # (owner-scoped via the workflow join).
    h24 = datetime.now(timezone.utc) - timedelta(hours=24)
    q = (
        select(ExecutionLog)
        .join(Workflow, Workflow.id == ExecutionLog.workflow_id)
        .where(ExecutionLog.started_at >= h24, ExecutionLog.status != "running")
        .limit(1000)
    )
    if user_id:
        q = q.where(Workflow.owner_id.in_([user_id, None]))
    rows_24h = (await db.execute(q)).scalars().all()
    runs_24h = len(rows_24h)
    failed_24h = sum(1 for ex in rows_24h if ex.status == "error")

    # SYSTEM verdict - degraded beats healthy, unhealthy beats both
    verdict = "healthy"
    if ds["unhealthy"] or pipes["failures_7d"] or deliveries["error_7d"] or incidents:
        verdict = "degraded"
    if (ds["unhealthy"] and pipes["failure_rate_7d"] >= 50) or \
            (pipes["failures_7d"] >= 10 and failed_24h >= 5):
        verdict = "unhealthy"

    return {
        "verdict": verdict,
        "generated_at": _iso(datetime.now(timezone.utc)),
        "workflows": {
            "total": pipes["workflows_total"],
            "active": pipes["active"],
            "running_now": running,
            "runs_24h": runs_24h,
            "failed_24h": failed_24h,
            "failures_7d": pipes["failures_7d"],
            "failure_rate_7d": pipes["failure_rate_7d"],
            "failing_workflows": pipes["failing_workflows"],
        },
        "datasets": ds,
        "reports": {
            "scheduled": reports_total,
            "dashboards": dashboards_total,
            "ok_7d": deliveries["ok_7d"],
            "error_7d": deliveries["error_7d"],
            "skipped_7d": deliveries["skipped_7d"],
            "last_error": deliveries["last_error"],
        },
        "agents": agents,
        "incidents": incidents,
    }


# ----------------------------------------------------------------- drilldown


def _failed_nodes(ex: ExecutionLog) -> list[dict]:
    """Node run records that errored WITHOUT a fallback save (the killers)."""
    return [
        r for r in (ex.node_runs or [])
        if r.get("status") == "error" and not r.get("fallback_used")
    ]


def _primary_failure(ex: ExecutionLog) -> dict | None:
    """The run-level killer: the first non-continued error node."""
    for r in _failed_nodes(ex):
        if not r.get("continued_on_fail"):
            return r
    return _failed_nodes(ex)[0] if _failed_nodes(ex) else None


async def _related_datasets(db: AsyncSession, wf: Workflow) -> list[Dataset]:
    """Datasets this workflow's graph touches (engine-resolution matching)."""
    ds_rows = (
        (await db.execute(select(Dataset).order_by(Dataset.updated_at.desc()).limit(300))).scalars().all()
    )
    related: list[Dataset] = []
    for ds in ds_rows:
        if len(related) >= 6:
            break
        if _graph_mentions(wf.graph, _view_names(ds), node_types=_DATA_NODE_TYPES):
            related.append(ds)
    return related


async def incident_chain(db: AsyncSession, user_id: str, execution_id: str) -> dict:
    """The full drilldown chain for one failed (or any) execution.

    Workflow -> execution -> node -> input -> error -> previous success ->
    related datasets (with health) -> impact. Derived on the spot.
    """
    ex = await db.get(ExecutionLog, execution_id)
    if ex is None:
        return {}
    wf = await db.get(Workflow, ex.workflow_id)
    if wf is None or (user_id and wf.owner_id not in (user_id, None)):
        return {}

    primary = _primary_failure(ex)
    failed = _failed_nodes(ex)

    # --- previous successful run: the baseline to compare against ----------
    prev = None
    if ex.started_at is not None:
        prev = (
            await db.execute(
                select(ExecutionLog)
                .where(
                    ExecutionLog.workflow_id == wf.id,
                    ExecutionLog.status == "success",
                    ExecutionLog.started_at < ex.started_at,
                )
                .order_by(ExecutionLog.started_at.desc())
                .limit(1)
            )
        ).scalars().first()

    comparison = None
    if prev is not None:
        prev_nodes = {r.get("node_id"): r for r in (prev.node_runs or [])}
        node_view = None
        if primary is not None:
            p = prev_nodes.get(primary.get("node_id"))
            node_view = {
                "present_in_previous": p is not None,
                "previous_status": (p or {}).get("status"),
                "previous_duration_ms": (p or {}).get("duration_ms"),
                "failed_duration_ms": primary.get("duration_ms"),
            }
        comparison = {
            "previous_execution_id": prev.id,
            "previous_started_at": _iso(prev.started_at),
            "previous_duration_ms": prev.duration_ms,
            "failed_duration_ms": ex.duration_ms,
            "previous_nodes": len(prev_nodes),
            "failed_nodes": len({r.get("node_id") for r in (ex.node_runs or [])}),
            "node": node_view,
        }

    # --- related datasets: health + impact (budget-capped) ------------------
    related = await _related_datasets(db, wf)
    datasets_out = []
    impacts = []
    for i, ds in enumerate(related):
        entry = {"id": ds.id, "name": ds.name, "rows": int(ds.row_count or 0), "ref": f"/datasets/{ds.id}"}
        if i < HEALTH_BUDGET:
            try:
                h = await compute_health(db, ds)
                entry["health"] = {"score": h["score"], "status": h["status"]}
            except Exception:
                entry["health"] = None
        datasets_out.append(entry)
        if i < IMPACT_BUDGET:
            try:
                impacts.append(await compute_impact(db, ds))
            except Exception:
                continue

    severity = "high" if failed else "info"
    if primary and primary.get("continued_on_fail"):
        severity = "medium"  # the flow survived on a continued-on-fail branch
    for imp in impacts:
        if imp.get("severity") in ("critical", "high"):
            severity = imp["severity"]
            break

    return {
        "execution": {
            "id": ex.id,
            "status": ex.status,
            "trigger_type": ex.trigger_type,
            "started_at": _iso(ex.started_at),
            "finished_at": _iso(ex.finished_at),
            "duration_ms": ex.duration_ms,
            "error": (ex.error or "")[:600] or None,
            "ref": f"/executions/{ex.id}",
        },
        "workflow": {"id": wf.id, "name": wf.name, "ref": f"/workflows/{wf.id}",
                     "active": bool(wf.is_active), "tags": list(wf.tags or [])},
        "failed_node": {
            **(primary or {}),
            "input": (primary or {}).get("input"),
        } if primary else None,
        "all_failed_nodes": [
            {"node_id": r.get("node_id"), "name": r.get("node_name"), "type": r.get("node_type"),
             "error": (r.get("error") or "")[:300]}
            for r in failed[:8]
        ],
        "comparison_with_previous_success": comparison,
        "related_datasets": datasets_out,
        "impact": impacts,
        "severity": severity,
        "chain": [
            {"step": "workflow", "label": wf.name, "ref": f"/workflows/{wf.id}"},
            {"step": "execution", "label": ex.id[:8], "ref": f"/executions/{ex.id}"},
            {"step": "node", "label": (primary or {}).get("node_name") or "none", "ref": None},
            {"step": "input", "label": "captured" if (primary or {}).get("input") is not None else "none", "ref": None},
            {"step": "error", "label": ((ex.error or "")[:120] or "none"), "ref": None},
            {"step": "previous_success", "label": prev.id[:8] if prev else "none on record", "ref": f"/executions/{prev.id}" if prev else None},
            {"step": "related_datasets", "label": ", ".join(d.name for d in related[:3]) or "none", "ref": f"/datasets/{related[0].id}" if related else None},
            {"step": "impact", "label": f"{sum(i['totals']['affected'] for i in impacts)} affected" if impacts else "none", "ref": None},
        ],
    }
