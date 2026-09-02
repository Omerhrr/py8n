"""APScheduler integration (Phase 4) - fires schedule_trigger nodes.

The scheduler lives inside the FastAPI lifespan and re-syncs jobs whenever a
workflow is saved/deleted/toggled, so the canvas is the single source of truth.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import func, select

from ..db import AsyncSessionLocal
from ..models import Workflow

logger = logging.getLogger("py8n.scheduler")

scheduler: AsyncIOScheduler | None = None


def job_id(workflow_id: str, node_id: str) -> str:
    return f"wf:{workflow_id}:{node_id}"


def start_scheduler() -> AsyncIOScheduler:
    global scheduler
    if scheduler is None:
        scheduler = AsyncIOScheduler(timezone="UTC")
        scheduler.start()
        logger.info("APScheduler started")
    return scheduler


async def shutdown_scheduler() -> None:
    global scheduler
    if scheduler is not None:
        scheduler.shutdown(wait=False)
        scheduler = None


async def _fire_scheduled_workflow(workflow_id: str, node_id: str) -> None:
    """Job callback: dispatch an execution for a schedule trigger node."""
    from .dispatcher import dispatch_execution

    async with AsyncSessionLocal() as session:
        wf = (
            await session.execute(select(Workflow).where(Workflow.id == workflow_id))
        ).scalar_one_or_none()
    if wf is None or not wf.is_active:
        return
    nodes = wf.schedule_nodes()
    if not any(n["id"] == node_id for n in nodes):
        return  # node was removed; job will be resynced by the save handler

    try:
        exec_id = await dispatch_execution(
            workflow_id,
            trigger_type="schedule",
            trigger_payload={
                "fired_at": datetime.now(timezone.utc).isoformat(),
                "node_id": node_id,
            },
            trigger_node_id=node_id,
        )
        logger.info("Scheduled execution %s fired for workflow %s", exec_id, workflow_id)
    except Exception:  # noqa: BLE001
        logger.exception("Scheduled execution failed for workflow %s", workflow_id)


async def _poll_dataset_trigger(workflow_id: str, node_id: str) -> None:
    """Job callback for dataset_trigger nodes (v50): fire on a new version.

    The cursor is an IngestionState row keyed ``trigger:{node_id}`` on the
    watched dataset. The FIRST poll records the current version without
    firing (activating a watcher must not stampede), and every poll after
    that fires exactly one run when the latest version moves past it. The
    cursor advance happens BEFORE dispatch so a slow run can never double-
    fire the same version; a FAILED run still consumed its trigger (the
    error-workflow binding / notification rules handle that case).
    """
    from sqlalchemy import select as _select

    from ..models import Dataset, DatasetVersion, IngestionState
    from .dispatcher import dispatch_execution
    from .ingestion import state_key, watermark_gt

    async with AsyncSessionLocal() as session:
        wf = (
            await session.execute(select(Workflow).where(Workflow.id == workflow_id))
        ).scalar_one_or_none()
        if wf is None or not wf.is_active:
            return
        node = next((n for n in wf.dataset_trigger_nodes() if n["id"] == node_id), None)
        if node is None:
            return  # node was removed; job will be resynced by the save handler
        params = node.get("parameters") or {}
        ref = str(params.get("dataset") or "").strip()
        if not ref:
            return
        ds = await _resolve_dataset_for_trigger(session, ref, wf.owner_id)
        if ds is None:
            logger.warning("dataset_trigger %s on workflow %s: dataset %r not found", node_id, workflow_id, ref)
            return
        latest = (
            await session.execute(
                _select(DatasetVersion)
                .where(DatasetVersion.dataset_id == ds.id)
                .order_by(DatasetVersion.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        key = state_key(f"trigger:{node_id}")
        st = (
            await session.execute(
                _select(IngestionState).where(
                    IngestionState.dataset_id == ds.id,
                    IngestionState.key == key,
                )
            )
        ).scalar_one_or_none()
        latest_v = str(latest.version) if latest is not None else None
        if st is None:
            st = IngestionState(
                dataset_id=ds.id,
                owner_id=wf.owner_id,
                key=key,
                watermark=latest_v,
            )
            session.add(st)
            await session.commit()
            return  # first sight: arm the watcher, do not fire
        if latest is None or not watermark_gt(latest_v, st.watermark):
            return  # nothing new
        st.watermark = latest_v
        st.runs = int(st.runs or 0) + 1
        st.rows_total = int(latest.row_count or 0)
        st.last_run_at = datetime.now(timezone.utc)
        await session.commit()

    try:
        exec_id = await dispatch_execution(
            workflow_id,
            trigger_type="dataset",
            trigger_payload={
                "dataset": ds.name,
                "dataset_id": ds.id,
                "version": int(latest.version),
                "row_count": int(latest.row_count or 0),
                "source": latest.source,
                "node_id": node_id,
            },
            trigger_node_id=node_id,
        )
        logger.info(
            "Dataset trigger fired: workflow %s on %s v%s (execution %s)",
            workflow_id, ds.name, latest.version, exec_id,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Dataset-trigger dispatch failed for workflow %s", workflow_id)


async def _resolve_dataset_for_trigger(session, ref: str, owner_id: str | None):
    """id-first, then case-insensitive name - same resolution as the engine."""
    from ..models import Dataset as _DS

    row = (
        await session.execute(select(_DS).where(_DS.id == ref))
    ).scalar_one_or_none()
    if row is not None:
        if owner_id is not None and row.owner_id not in (None, owner_id):
            return None
        return row
    row = (
        await session.execute(
            select(_DS).where(func.lower(_DS.name) == ref.lower())
        )
    ).scalar_one_or_none()
    if row is not None and owner_id is not None and row.owner_id not in (None, owner_id):
        return None
    return row


async def resync_workflow_jobs(workflow_id: str) -> None:
    """Re-register APScheduler jobs for one workflow's schedule + dataset
    trigger nodes."""
    if scheduler is None:
        return
    sched = scheduler
    # Remove any existing jobs for this workflow
    for job in list(sched.get_jobs()):
        if job.id.startswith(f"wf:{workflow_id}:"):
            job.remove()

    async with AsyncSessionLocal() as session:
        wf = (
            await session.execute(select(Workflow).where(Workflow.id == workflow_id))
        ).scalar_one_or_none()
    if wf is None or not wf.is_active:
        return

    for node in wf.schedule_nodes():
        params = node.get("parameters") or {}
        mode = params.get("mode", "interval")
        try:
            if mode == "cron":
                trigger = CronTrigger.from_crontab(params.get("cron") or "*/5 * * * *", timezone="UTC")
            else:
                trigger = IntervalTrigger(seconds=max(5, int(params.get("interval_seconds") or 300)))
            sched.add_job(
                _fire_scheduled_workflow,
                trigger=trigger,
                id=job_id(workflow_id, node["id"]),
                args=[workflow_id, node["id"]],
                replace_existing=True,
                misfire_grace_time=30,
            )
            logger.info("Registered schedule job %s (%s)", job_id(workflow_id, node["id"]), mode)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not register schedule job for node %s: %s", node["id"], exc)

    # v50: dataset watchers - one polling job per dataset_trigger node
    for node in wf.dataset_trigger_nodes():
        params = node.get("parameters") or {}
        try:
            seconds = max(30, int(params.get("poll_seconds") or 60))
            sched.add_job(
                _poll_dataset_trigger,
                trigger=IntervalTrigger(seconds=seconds),
                id=job_id(workflow_id, node["id"]),
                args=[workflow_id, node["id"]],
                replace_existing=True,
                misfire_grace_time=60,
            )
            logger.info("Registered dataset-trigger job %s (every %ss)", job_id(workflow_id, node["id"]), seconds)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not register dataset-trigger job for node %s: %s", node["id"], exc)


async def resync_all_jobs() -> None:
    """On startup: register jobs for every active workflow with schedule nodes."""
    async with AsyncSessionLocal() as session:
        workflows = (
            await session.execute(select(Workflow).where(Workflow.is_active.is_(True)))
        ).scalars().all()
    for wf in workflows:
        await resync_workflow_jobs(wf.id)


async def _fire_scheduled_report(report_id: str) -> None:
    """Job callback for scheduled report exports (v48).

    reports.run_report owns its session and never raises - a failing report
    only marks its own row - so the scheduler cannot be wedged by a bad job.
    """
    from .reports import run_report

    try:
        result = await run_report(report_id)
        if result.get("ok"):
            logger.info("Scheduled report %s exported (artifact %s)", report_id, result.get("artifact_id"))
        else:
            logger.warning("Scheduled report %s failed: %s", report_id, result.get("error"))
    except Exception:  # noqa: BLE001 - belt and braces
        logger.exception("Scheduled report job %s crashed", report_id)


async def resync_report_jobs(report_id: str) -> None:
    """Re-register (or clear) the APScheduler job for one scheduled report."""
    if scheduler is None:
        return
    job_id = f"report:{report_id}"
    for job in list(scheduler.get_jobs()):
        if job.id == job_id:
            job.remove()

    from ..models import ScheduledReport

    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(select(ScheduledReport).where(ScheduledReport.id == report_id))
        ).scalar_one_or_none()
    if row is None or not row.enabled:
        return
    try:
        trigger = CronTrigger.from_crontab(row.cron or "0 6 * * *", timezone="UTC")
        scheduler.add_job(
            _fire_scheduled_report,
            trigger=trigger,
            id=job_id,
            args=[report_id],
            replace_existing=True,
            misfire_grace_time=120,
        )
        logger.info("Registered report job %s (cron %s)", job_id, row.cron)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not register report job %s: %s", report_id, exc)


async def resync_all_report_jobs() -> None:
    """On startup: register jobs for every enabled scheduled report (v48)."""
    from ..models import ScheduledReport

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(select(ScheduledReport).where(ScheduledReport.enabled.is_(True)))
        ).scalars().all()
    for row in rows:
        await resync_report_jobs(row.id)


# ----------------------------------------------------------------------
# Schedule introspection (v7) - validation, human summaries, fire previews
# ----------------------------------------------------------------------
def _build_trigger(params: dict):
    """Build the APScheduler trigger for a schedule node's parameters.

    Raises ValueError/TypeError when the parameters cannot form a valid
    schedule (e.g. a malformed crontab expression).
    """
    mode = params.get("mode", "interval")
    if mode == "cron":
        return CronTrigger.from_crontab(params.get("cron") or "*/5 * * * *", timezone="UTC")
    return IntervalTrigger(seconds=max(5, int(params.get("interval_seconds") or 300)))


def validate_schedule_params(params: dict) -> None:
    """Raise ValueError/TypeError when a schedule node's params are unschedulable."""
    _build_trigger(params)


def describe_schedule(params: dict) -> str:
    """One-line human summary, e.g. ``cron 0 9 * * 1-5`` or ``every 5m``."""
    mode = params.get("mode", "interval")
    if mode == "cron":
        return f"cron {params.get('cron') or '*/5 * * * *'}"
    try:
        seconds = max(5, int(params.get("interval_seconds") or 300))
    except (TypeError, ValueError):
        seconds = 300
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"every {hours}h" if hours > 1 else "hourly"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"every {minutes}m" if minutes > 1 else "every minute"
    return f"every {seconds}s"


def next_fire_times(params: dict, count: int = 5) -> list[str]:
    """ISO-UTC previews of the next ``count`` fire times ([] when invalid).

    Note: APScheduler's ``get_next_fire_time(previous, now)`` computes
    ``min(now, previous + 1µs)`` for cron triggers, so passing a previous
    fire time that is ahead of ``now`` re-derives the SAME slot. To walk the
    future we instead advance ``now`` just past each computed fire time.
    """
    try:
        trigger = _build_trigger(params)
    except (ValueError, TypeError):
        return []
    out: list[str] = []
    now = datetime.now(timezone.utc)
    for _ in range(max(1, count)):
        try:
            nxt = trigger.get_next_fire_time(None, now)
        except Exception:  # noqa: BLE001 - defensive: malformed expressions
            break
        if nxt is None:
            break
        out.append(nxt.isoformat())
        now = nxt + timedelta(microseconds=1)
    return out


def schedule_entries_for_graph(graph: dict | None) -> list[dict]:
    """Describe every schedule_trigger node in a graph dict.

    Each entry carries the node identity, a human summary and the next fire
    previews; ``error`` is set (and previews empty) when the node's params
    cannot be scheduled.
    """
    entries: list[dict] = []
    for node in (graph or {}).get("nodes", []):
        if node.get("type") != "schedule_trigger":
            continue
        params = node.get("parameters") or {}
        error: str | None = None
        try:
            validate_schedule_params(params)
        except (ValueError, TypeError) as exc:
            error = str(exc) or exc.__class__.__name__
        entries.append(
            {
                "node_id": node.get("id"),
                "node_name": node.get("name") or "Schedule",
                "mode": params.get("mode", "interval"),
                "cron": params.get("cron"),
                "interval_seconds": params.get("interval_seconds"),
                "summary": describe_schedule(params),
                "next_runs": next_fire_times(params, 5) if error is None else [],
                "error": error,
            }
        )
    return entries
