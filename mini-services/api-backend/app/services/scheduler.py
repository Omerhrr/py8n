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
from sqlalchemy import select

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


async def resync_workflow_jobs(workflow_id: str) -> None:
    """Re-register APScheduler jobs for one workflow's schedule nodes."""
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


async def resync_all_jobs() -> None:
    """On startup: register jobs for every active workflow with schedule nodes."""
    async with AsyncSessionLocal() as session:
        workflows = (
            await session.execute(select(Workflow).where(Workflow.is_active.is_(True)))
        ).scalars().all()
    for wf in workflows:
        await resync_workflow_jobs(wf.id)


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
