"""Data observability (v53) - one derived stream of everything that happened.

Py8n already RECORDS the truth an observability surface needs, it just
records it in the table that owns the domain: dataset versions are the
write log, execution logs are the run history, delivery events are the
push-out trail, the audit tables are the share-surface door log. This
module stitches those tables into ONE unified, owner-scoped event stream
plus a fleet-wide overview - derived, never stored, so it can never
drift from what actually happened.

Event envelope (every source maps into this shape)::

    {"id": "<source>:<row-id>", "type": "...", "ts": ISO, "severity": ...,
     "title": "...", "detail": "...", "ref": "/deep/link", "meta": {...}}

Severities: ``info`` (things that happened), ``warn`` (worth a look:
denied share attempts, skipped deliveries), ``error`` (incidents:
failed runs, failed deliveries, cancelled runs).

The overview composes the same primitives the per-dataset health report
uses (:mod:`services.health`) across the whole estate, with a profiling
budget so a large estate cannot make the endpoint expensive: at most
``HEALTH_BUDGET`` datasets get a full health score per call (newest
first), the rest are counted as ``unscored``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    App,
    Dashboard,
    DashboardAuditEvent,
    Dataset,
    DatasetContract,
    DatasetVersion,
    ExecutionLog,
    GrantAuditEvent,
    IngestionState,
    ReportDeliveryEvent,
    ScheduledReport,
    Workflow,
)
from .health import compute_health

HEALTH_BUDGET = 40          # max datasets fully health-scored per overview call
DEFAULT_EVENT_LIMIT = 100
MAX_EVENT_LIMIT = 500
MAX_PER_SOURCE = 500        # per-source query cap before the merge

SEVERITY_ORDER = {"error": 0, "warn": 1, "info": 2}


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes - normalize for comparisons."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def _names_by_id(db: AsyncSession, model, ids: list[str]) -> dict[str, str]:
    if not ids:
        return {}
    rows = (await db.execute(select(model.id, model.name).where(model.id.in_(set(ids))))).all()
    return {r[0]: r[1] for r in rows}


# --------------------------------------------------------------------- events

async def _dataset_events(db: AsyncSession, user_id: str | None, since: datetime) -> list[dict]:
    q = (
        select(DatasetVersion, Dataset.name)
        .join(Dataset, Dataset.id == DatasetVersion.dataset_id)
        .where(DatasetVersion.created_at >= since)
        .order_by(DatasetVersion.created_at.desc())
        .limit(MAX_PER_SOURCE)
    )
    if user_id:
        q = q.where(Dataset.owner_id.in_([user_id, None]))
    rows = (await db.execute(q)).all()
    wf_ids = [r[0].workflow_id for r in rows if r[0].workflow_id]
    wf_names = await _names_by_id(db, Workflow, wf_ids)
    out = []
    for ver, ds_name in rows:
        producer = wf_names.get(ver.workflow_id) if ver.workflow_id else None
        title = f"{ds_name} v{ver.version} written ({ver.row_count} rows, {ver.source})"
        if producer and ver.node_name:
            title += f" by {producer} · {ver.node_name}"
        elif producer:
            title += f" by {producer}"
        out.append({
            "id": f"dsver:{ver.id}",
            "type": "dataset.written",
            "ts": _iso(ver.created_at),
            "severity": "info",
            "title": title,
            "detail": f"source={ver.source}" + (f", producer={producer}" if producer else ""),
            "ref": f"/datasets/{ver.dataset_id}",
            "meta": {
                "dataset_id": ver.dataset_id,
                "dataset": ds_name,
                "version": int(ver.version or 0),
                "row_count": int(ver.row_count or 0),
                "write_source": ver.source,
                "producer": producer,
                "node_name": ver.node_name,
            },
        })
    return out


_CONTRACT_MARKER = "data contract violated"


async def _execution_events(db: AsyncSession, user_id: str | None, since: datetime) -> list[dict]:
    q = (
        select(ExecutionLog, Workflow.name)
        .join(Workflow, Workflow.id == ExecutionLog.workflow_id)
        .where(ExecutionLog.started_at >= since, ExecutionLog.status != "running")
        .order_by(ExecutionLog.started_at.desc())
        .limit(MAX_PER_SOURCE)
    )
    if user_id:
        q = q.where(Workflow.owner_id.in_([user_id, None]))
    rows = (await db.execute(q)).all()
    out = []
    for ex, wf_name in rows:
        status = ex.status or "success"
        if status == "error":
            etype, severity = "workflow.failed", "error"
        elif status == "cancelled":
            etype, severity = "workflow.cancelled", "warn"
        else:
            etype, severity = "workflow.succeeded", "info"
        err = (ex.error or "")[:300] or None
        title = f"{wf_name} {etype.split('.')[1]}" + (f" - {err}" if err and severity == "error" else "")
        meta = {
            "workflow_id": ex.workflow_id,
            "workflow": wf_name,
            "execution_id": ex.id,
            "trigger_type": ex.trigger_type,
            "duration_ms": ex.duration_ms,
        }
        if err and _CONTRACT_MARKER in (ex.error or ""):
            meta["category"] = "contract"
        out.append({
            "id": f"exec:{ex.id}",
            "type": etype,
            "ts": _iso(ex.started_at),
            "severity": severity,
            "title": title[:300],
            "detail": err,
            "ref": f"/executions/{ex.id}",
            "meta": meta,
        })
    return out


async def _delivery_events(db: AsyncSession, user_id: str | None, since: datetime) -> list[dict]:
    q = (
        select(ReportDeliveryEvent, ScheduledReport.name)
        .join(ScheduledReport, ScheduledReport.id == ReportDeliveryEvent.report_id)
        .where(ReportDeliveryEvent.created_at >= since)
        .order_by(ReportDeliveryEvent.created_at.desc())
        .limit(MAX_PER_SOURCE)
    )
    if user_id:
        q = q.where(ScheduledReport.owner_id.in_([user_id, None]))
    rows = (await db.execute(q)).all()
    out = []
    for ev, report_name in rows:
        if ev.status == "error":
            etype, severity = "report.delivery_failed", "error"
        elif ev.status == "skipped":
            etype, severity = "report.delivery_skipped", "warn"
        else:
            etype, severity = "report.delivered", "info"
        out.append({
            "id": f"delivery:{ev.id}",
            "type": etype,
            "ts": _iso(ev.created_at),
            "severity": severity,
            "title": f"report {report_name!r} {ev.channel} -> {ev.target or '(no target)'}: {ev.status}",
            "detail": ev.detail,
            "ref": "/reports",
            "meta": {
                "report_id": ev.report_id,
                "report": report_name,
                "channel": ev.channel,
                "target": ev.target,
                "attached": bool(ev.attached),
            },
        })
    return out


async def _share_denied_events(db: AsyncSession, user_id: str | None, since: datetime) -> list[dict]:
    out: list[dict] = []
    qa = (
        select(GrantAuditEvent, App.name)
        .join(App, App.id == GrantAuditEvent.app_id)
        .where(GrantAuditEvent.created_at >= since, GrantAuditEvent.outcome == "denied")
        .order_by(GrantAuditEvent.created_at.desc())
        .limit(MAX_PER_SOURCE)
    )
    if user_id:
        qa = qa.where(App.owner_id.in_([user_id, None]))
    for ev, app_name in (await db.execute(qa)).all():
        out.append({
            "id": f"grantaudit:{ev.id}",
            "type": "share.denied",
            "ts": _iso(ev.created_at),
            "severity": "warn",
            "title": f"app {app_name!r} share access denied ({ev.action})",
            "detail": ev.detail,
            "ref": "/apps",
            "meta": {"app_id": ev.app_id, "app": app_name, "action": ev.action,
                     "grant_name": ev.grant_name, "surface": "app"},
        })
    qd = (
        select(DashboardAuditEvent, Dashboard.name)
        .join(Dashboard, Dashboard.id == DashboardAuditEvent.dashboard_id)
        .where(DashboardAuditEvent.created_at >= since, DashboardAuditEvent.outcome == "denied")
        .order_by(DashboardAuditEvent.created_at.desc())
        .limit(MAX_PER_SOURCE)
    )
    if user_id:
        qd = qd.where(Dashboard.owner_id.in_([user_id, None]))
    for ev, dash_name in (await db.execute(qd)).all():
        out.append({
            "id": f"dashaudit:{ev.id}",
            "type": "share.denied",
            "ts": _iso(ev.created_at),
            "severity": "warn",
            "title": f"dashboard {dash_name!r} share access denied ({ev.action})",
            "detail": ev.detail,
            "ref": "/dashboards",
            "meta": {"dashboard_id": ev.dashboard_id, "dashboard": dash_name,
                     "action": ev.action, "surface": "dashboard"},
        })
    return out


SOURCE_TYPES = {
    "dataset.written": _dataset_events,
    "workflow.failed": _execution_events,
    "workflow.succeeded": _execution_events,
    "workflow.cancelled": _execution_events,
    "report.delivered": _delivery_events,
    "report.delivery_failed": _delivery_events,
    "report.delivery_skipped": _delivery_events,
    "share.denied": _share_denied_events,
}


async def list_events(
    db: AsyncSession,
    user_id: str,
    types: list[str] | None = None,
    severity: str | None = None,
    hours: float = 168.0,
    limit: int = DEFAULT_EVENT_LIMIT,
    offset: int = 0,
) -> dict:
    """The unified event stream - derived from the domain tables.

    ``hours`` bounds how far back each source is queried (default 7d);
    ``types`` filters after the merge (prefix matching, so
    ``workflow.`` catches succeeded+failed+cancelled).
    """
    limit = max(1, min(int(limit or DEFAULT_EVENT_LIMIT), MAX_EVENT_LIMIT))
    offset = max(0, int(offset or 0))
    since = datetime.now(timezone.utc) - timedelta(hours=max(0.1, float(hours or 168)))

    # Prefix matching: "workflow." catches succeeded+failed+cancelled.
    all_fetchers = list({SOURCE_TYPES[t] for t in SOURCE_TYPES})
    if types:
        wanted = {t.strip() for t in types if t.strip()}
        matched_types = {kt for kt in SOURCE_TYPES if any(kt == w or kt.startswith(w) for w in wanted)}
        fetchers = list({SOURCE_TYPES[t] for t in matched_types})
    else:
        wanted = None
        fetchers = all_fetchers
    if severity == "error":
        # failed runs + failed deliveries only - skip the info-only fetcher
        fetchers = [f for f in fetchers if f is not _dataset_events]

    events: list[dict] = []
    for fetch in fetchers:
        try:
            events.extend(await fetch(db, user_id, since))
        except Exception:  # a broken source must never blank the whole stream
            continue

    if wanted:
        events = [e for e in events if any(e["type"] == w or e["type"].startswith(w) for w in wanted)]
    if severity in ("info", "warn", "error"):
        events = [e for e in events if e["severity"] == severity]

    events.sort(key=lambda e: (e["ts"] or "", SEVERITY_ORDER.get(e["severity"], 2)), reverse=True)
    total = len(events)
    return {
        "events": events[offset:offset + limit],
        "total": total,
        "limit": limit,
        "offset": offset,
        "since": _iso(since),
        "types": sorted(SOURCE_TYPES),
    }


# -------------------------------------------------------------------- overview

async def _dataset_overview(db: AsyncSession, user_id: str) -> dict:
    ds_rows = (
        (await db.execute(select(Dataset).order_by(Dataset.updated_at.desc()))).scalars().all()
    )
    visible = [d for d in ds_rows if d.owner_id in (user_id, None)]
    total = len(visible)
    rows_total = sum(int(d.row_count or 0) for d in visible)

    contract_rows = (
        (await db.execute(select(DatasetContract))).scalars().all()
    )
    contracts = {c.dataset_id: c for c in contract_rows if c.owner_id in (user_id, None)}

    counts = {"healthy": 0, "degraded": 0, "unhealthy": 0}
    violating = 0
    stale = 0
    worst: dict | None = None
    scored = 0
    for ds in visible[:HEALTH_BUDGET]:
        try:
            h = await compute_health(db, ds)
        except Exception:
            continue  # unreadable blob etc. - skip, never break the overview
        scored += 1
        counts[h["status"]] = counts.get(h["status"], 0) + 1
        if h["schema"]["contract_ok"] is False:
            violating += 1
        if h["freshness"]["tier"] in ("stale", "cold"):
            stale += 1
        if worst is None or h["score"] < worst["score"]:
            worst = {
                "dataset_id": ds.id,
                "name": ds.name,
                "score": h["score"],
                "status": h["status"],
                "ref": f"/datasets/{ds.id}",
            }
    return {
        "total": total,
        "scored": scored,
        "unscored": total - scored,
        **counts,
        "violating_contracts": violating,
        "contracts_total": len(contracts),
        "stale_or_cold": stale,
        "rows_total": rows_total,
        "worst": worst,
    }


async def _pipeline_overview(db: AsyncSession, user_id: str) -> dict:
    now = datetime.now(timezone.utc)
    h24 = now - timedelta(hours=24)
    d7 = now - timedelta(days=7)

    wfs = (
        (await db.execute(select(Workflow).where(Workflow.owner_id.in_([user_id, None])))).scalars().all()
        if user_id else (await db.execute(select(Workflow))).scalars().all()
    )
    run_rows = (
        await db.execute(
            select(
                ExecutionLog.workflow_id,
                ExecutionLog.status,
                ExecutionLog.started_at,
                ExecutionLog.error,
                ExecutionLog.finished_at,
            ).where(ExecutionLog.started_at >= d7)
        )
    ).all()
    visible_wf_ids = {w.id for w in wfs}
    runs_7d = [r for r in run_rows if r[0] in visible_wf_ids or not user_id]
    runs_24h = sum(1 for r in runs_7d if _aware(r[2]) and _aware(r[2]) >= h24)
    failures = [r for r in runs_7d if r[1] == "error"]
    failure_rate = round(len(failures) / len(runs_7d) * 100, 1) if runs_7d else 0.0

    per_wf: dict[str, dict] = {}
    for wf_id, status, started_at, error, finished_at in failures:
        entry = per_wf.setdefault(wf_id, {"failures": 0, "last_error": None, "last_failed_at": None})
        entry["failures"] += 1
        if entry["last_failed_at"] is None or (_aware(started_at) or started_at) > _aware(entry["last_failed_at"]):
            entry["last_error"] = (error or "")[:200] or None
            entry["last_failed_at"] = started_at
    names = await _names_by_id(db, Workflow, list(per_wf))
    failing = sorted(
        (
            {
                "workflow_id": wid,
                "name": names.get(wid, wid[:8]),
                "ref": f"/workflows/{wid}",
                **entry,
                "last_failed_at": _iso(entry["last_failed_at"]),
            }
            for wid, entry in per_wf.items()
        ),
        key=lambda x: x["last_failed_at"] or "",
        reverse=True,
    )[:5]
    return {
        "workflows_total": len(wfs),
        "active": sum(1 for w in wfs if w.is_active),
        "runs_24h": runs_24h,
        "runs_7d": len(runs_7d),
        "failures_7d": len(failures),
        "failure_rate_7d": failure_rate,
        "failing_workflows": failing,
    }


async def _ingestion_overview(db: AsyncSession, user_id: str) -> dict:
    q = select(IngestionState).where(~IngestionState.key.like("trigger:%"))
    if user_id:
        q = q.where(IngestionState.owner_id.in_([user_id, None]))
    states = (await db.execute(q.order_by(IngestionState.last_run_at.desc()))).scalars().all()
    ds_names = await _names_by_id(db, Dataset, [s.dataset_id for s in states])
    now = datetime.now(timezone.utc)
    active_24h = sum(
        1 for s in states
        if s.last_run_at and (s.last_run_at if s.last_run_at.tzinfo else s.last_run_at.replace(tzinfo=timezone.utc)) >= now - timedelta(hours=24)
    )
    return {
        "checkpoints": len(states),
        "rows_total": sum(int(s.rows_total or 0) for s in states),
        "active_24h": active_24h,
        "pipelines": [
            {
                "dataset": ds_names.get(s.dataset_id, s.dataset_id[:8]),
                "dataset_id": s.dataset_id,
                "ref": f"/datasets/{s.dataset_id}",
                "key": s.key,
                "watermark": s.watermark,
                "runs": int(s.runs or 0),
                "rows_total": int(s.rows_total or 0),
                "last_run_at": _iso(s.last_run_at),
                "stats": s.stats_json,
            }
            for s in states[:8]
        ],
    }


async def _delivery_overview(db: AsyncSession, user_id: str) -> dict:
    d7 = datetime.now(timezone.utc) - timedelta(days=7)
    q = (
        select(ReportDeliveryEvent)
        .where(ReportDeliveryEvent.created_at >= d7)
        .order_by(ReportDeliveryEvent.created_at.desc())
    )
    if user_id:
        q = q.join(ScheduledReport, ScheduledReport.id == ReportDeliveryEvent.report_id).where(
            ScheduledReport.owner_id.in_([user_id, None])
        )
    events = (await db.execute(q)).scalars().all()
    last_error = next((e for e in events if e.status == "error"), None)
    return {
        "ok_7d": sum(1 for e in events if e.status == "ok"),
        "error_7d": sum(1 for e in events if e.status == "error"),
        "skipped_7d": sum(1 for e in events if e.status == "skipped"),
        "last_error": (last_error.detail or last_error.target) if last_error else None,
    }


async def build_overview(db: AsyncSession, user_id: str) -> dict:
    ds = await _dataset_overview(db, user_id)
    pipes = await _pipeline_overview(db, user_id)
    ing = await _ingestion_overview(db, user_id)
    deliveries = await _delivery_overview(db, user_id)

    stream = await list_events(db, user_id, severity="error", hours=72, limit=10, offset=0)
    incidents = [
        {
            "id": e["id"],
            "type": e["type"],
            "ts": e["ts"],
            "severity": e["severity"],
            "title": e["title"],
            "detail": e["detail"],
            "ref": e["ref"],
        }
        for e in stream["events"]
    ]
    overall = "healthy"
    if ds["unhealthy"] or pipes["failures_7d"]:
        overall = "degraded"
    if ds["violating_contracts"] and ds["unhealthy"] and pipes["failure_rate_7d"] >= 50:
        overall = "unhealthy"
    return {
        "overall": overall,
        "generated_at": _iso(datetime.now(timezone.utc)),
        "datasets": ds,
        "pipelines": pipes,
        "ingestion": ing,
        "deliveries": deliveries,
        "incidents": incidents,
    }
