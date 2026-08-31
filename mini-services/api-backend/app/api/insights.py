"""Aggregated execution insights - platform-level analytics for operators.

Single read-only endpoint that rolls the execution log up into:

* ``summary``           - status counts, success rate, avg duration
* ``timeline``          - per-day buckets (zero-filled) for the window
* ``top_workflows``     - leaderboard by run count (with error + duration)
* ``node_stats``        - per-node-type aggregates from persisted node runs
* ``trigger_breakdown`` - manual / webhook / schedule / error split

All queries are read-only; aggregation happens in SQL where trivial and in
Python for the JSON ``node_runs`` column (SQLite has no JSON aggregation and
the sandbox dataset is small).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import ExecutionLog, Workflow

router = APIRouter(prefix="/insights", tags=["insights"])


def _pct(part: int, whole: int) -> float:
    return round(part / whole * 100, 1) if whole else 0.0


@router.get("")
async def get_insights(
    days: int = Query(default=14, ge=1, le=90, description="Window length in days (incl. today)"),
    workflow_id: str | None = Query(default=None, description="Scope to a single workflow"),
    db: AsyncSession = Depends(get_db),
):
    """Aggregate execution analytics over the trailing ``days``-day window.

    The window is calendar-aligned: ``days`` UTC buckets ending today, so the
    timeline, summary and node stats always cover exactly the same period.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff_date = (now - timedelta(days=days - 1)).date()
    since = datetime.combine(cutoff_date, time.min)  # naive-UTC, matches stored rows

    stmt = select(ExecutionLog).where(ExecutionLog.started_at >= since)
    if workflow_id:
        stmt = stmt.where(ExecutionLog.workflow_id == workflow_id)
    rows = (await db.execute(stmt)).scalars().all()

    # ------------------------------------------------------ summary
    by_status = Counter(r.status or "running" for r in rows)
    durations = [r.duration_ms for r in rows if r.duration_ms is not None]
    node_runs_total = sum(len(r.node_runs or []) for r in rows)
    summary = {
        "total": len(rows),
        "success": by_status.get("success", 0),
        "error": by_status.get("error", 0),
        "waiting": by_status.get("waiting", 0),
        "cancelled": by_status.get("cancelled", 0),
        "running": by_status.get("running", 0),
        # Success rate over *finished* runs - pending/waiting/running runs
        # would only dilute the signal operators care about.
        "success_rate": _pct(by_status.get("success", 0),
                             by_status.get("success", 0) + by_status.get("error", 0)),
        "avg_duration_ms": int(sum(durations) / len(durations)) if durations else 0,
        "node_runs_total": node_runs_total,
    }

    # ------------------------------------------------------ timeline (zero-filled)
    timeline: list[dict] = []
    buckets: dict[str, dict] = {}
    for i in range(days):
        d = (cutoff_date + timedelta(days=i)).isoformat()
        bucket = {"date": d, "total": 0, "success": 0, "error": 0, "waiting": 0, "cancelled": 0, "running": 0}
        buckets[d] = bucket
        timeline.append(bucket)
    for r in rows:
        key = r.started_at.date().isoformat()
        if key in buckets:  # defensive: rows are window-filtered already
            buckets[key]["total"] += 1
            buckets[key][r.status or "running"] += 1

    # ------------------------------------------------------ top workflows
    per_wf: dict[str, dict] = {}
    for r in rows:
        slot = per_wf.setdefault(
            r.workflow_id,
            {"workflow_id": r.workflow_id, "runs": 0, "success": 0, "errors": 0, "durations": []},
        )
        slot["runs"] += 1
        if r.status == "success":
            slot["success"] += 1
        elif r.status == "error":
            slot["errors"] += 1
        if r.duration_ms is not None:
            slot["durations"].append(r.duration_ms)
    wf_ids = set(per_wf)
    names: dict[str, str] = {}
    if wf_ids:
        name_rows = (
            await db.execute(select(Workflow.id, Workflow.name).where(Workflow.id.in_(wf_ids)))
        ).all()
        names = dict(name_rows)
    top_workflows = [
        {
            "workflow_id": w["workflow_id"],
            "workflow_name": names.get(w["workflow_id"], w["workflow_id"]),
            "runs": w["runs"],
            "success": w["success"],
            "errors": w["errors"],
            "success_rate": _pct(w["success"], w["runs"]),
            "avg_duration_ms": int(sum(w["durations"]) / len(w["durations"])) if w["durations"] else 0,
        }
        for w in sorted(per_wf.values(), key=lambda w: (-w["runs"], w["workflow_id"]))[:8]
    ]

    # ------------------------------------------------------ node stats
    agg: dict[str, dict] = defaultdict(lambda: {"runs": 0, "errors": 0, "skipped": 0, "durations": []})
    for r in rows:
        for run in r.node_runs or []:
            ntype = run.get("node_type")
            # internal helpers (e.g. _batch_trigger injected into loop bodies)
            # are engine plumbing, not operator-visible node types
            if not ntype or ntype.startswith("_"):
                continue
            slot = agg[ntype]
            slot["runs"] += 1
            if run.get("status") == "error":
                slot["errors"] += 1
            elif run.get("status") == "skipped":
                slot["skipped"] += 1
            if run.get("duration_ms") is not None:
                slot["durations"].append(run["duration_ms"])
    node_stats = [
        {
            "node_type": ntype,
            "runs": slot["runs"],
            "errors": slot["errors"],
            "skipped": slot["skipped"],
            "error_rate": _pct(slot["errors"], slot["runs"]),
            "avg_duration_ms": int(sum(slot["durations"]) / len(slot["durations"])) if slot["durations"] else 0,
        }
        for ntype, slot in sorted(agg.items(), key=lambda kv: -kv[1]["runs"])[:12]
    ]

    # ------------------------------------------------------ triggers
    triggers = Counter(r.trigger_type or "manual" for r in rows)

    return {
        "window": {
            "days": days,
            "since": since.isoformat(),
            "until": now.isoformat(),
            "workflow_id": workflow_id,
        },
        "summary": summary,
        "timeline": timeline,
        "top_workflows": top_workflows,
        "node_stats": node_stats,
        "trigger_breakdown": dict(triggers),
    }
