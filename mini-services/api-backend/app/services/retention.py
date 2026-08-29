"""Execution data retention (v19).

A single AppSetting row (key = ``execution_retention``) stores the policy:

    {
        "retention_days": 30,                # 0 = keep forever
        "max_executions_per_workflow": 500,  # 0 = unlimited
        "last_purge_at": "2026-…",           # bookkeeping, set by purge()
        "last_purge_deleted": 12             # bookkeeping, set by purge()
    }

``purge_execution_data`` enforces both knobs in one pass:
* age-based: every FINISHED execution older than ``retention_days`` is deleted
  (running executions are never touched);
* volume-based: per workflow, only the newest ``max_executions_per_workflow``
  finished executions survive.

Explicit commits everywhere — the yield-dependency teardown commit runs after
the response is sent, so background/purge writes must commit themselves.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from ..db import AsyncSessionLocal
from ..models import AppSetting, ExecutionLog

logger = logging.getLogger("py8n.retention")

SETTING_KEY = "execution_retention"

DEFAULTS = {
    "retention_days": 30,
    "max_executions_per_workflow": 0,
    "last_purge_at": None,
    "last_purge_deleted": 0,
}


async def get_policy() -> dict:
    async with AsyncSessionLocal() as session:
        row = await session.get(AppSetting, SETTING_KEY)
    policy = dict(DEFAULTS)
    if row and isinstance(row.value, dict):
        for key in ("retention_days", "max_executions_per_workflow"):
            if isinstance(row.value.get(key), int) and row.value[key] >= 0:
                policy[key] = row.value[key]
        policy["last_purge_at"] = row.value.get("last_purge_at")
        policy["last_purge_deleted"] = row.value.get("last_purge_deleted", 0)
    return policy


def schedule_daily_purge() -> None:
    """Register the daily purge on the app's APScheduler (no-op if scheduler absent)."""
    from .scheduler import scheduler

    if scheduler is not None:
        scheduler.add_job(
            purge_execution_data,
            "interval",
            hours=24,
            id="retention-purge",
            replace_existing=True,
        )
        logger.info("daily retention purge scheduled (every 24h)")


async def set_policy(patch: dict) -> dict:
    current = await get_policy()
    for key in ("retention_days", "max_executions_per_workflow"):
        if key in patch and patch[key] is not None:
            value = int(patch[key])
            if value < 0:
                raise ValueError(f"{key} must be >= 0")
            current[key] = value
    async with AsyncSessionLocal() as session:
        row = await session.get(AppSetting, SETTING_KEY)
        if row is None:
            row = AppSetting(key=SETTING_KEY, value=current)
            session.add(row)
        else:
            row.value = current
        await session.commit()  # explicit — teardown commit is too late
    return current


async def purge_execution_data() -> dict:
    """Apply the retention policy. Returns {deleted_by_age, deleted_by_volume, total}."""
    policy = await get_policy()
    days = policy["retention_days"]
    cap = policy["max_executions_per_workflow"]
    deleted_by_age = 0
    deleted_by_volume = 0

    async with AsyncSessionLocal() as session:
        if days > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            result = await session.execute(
                delete(ExecutionLog).where(
                    ExecutionLog.status != "running",
                    ExecutionLog.finished_at.is_not(None),
                    ExecutionLog.finished_at < cutoff,
                )
            )
            deleted_by_age = result.rowcount or 0

        if cap > 0:
            workflow_ids = (
                await session.execute(select(ExecutionLog.workflow_id).distinct())
            ).scalars().all()
            for workflow_id in workflow_ids:
                keep_ids = (
                    await session.execute(
                        select(ExecutionLog.id)
                        .where(ExecutionLog.workflow_id == workflow_id, ExecutionLog.status != "running")
                        .order_by(ExecutionLog.started_at.desc())
                        .limit(cap)
                    )
                ).scalars().all()
                if len(keep_ids) < cap:
                    continue  # under the cap — nothing to do
                result = await session.execute(
                    delete(ExecutionLog).where(
                        ExecutionLog.workflow_id == workflow_id,
                        ExecutionLog.status != "running",
                        ExecutionLog.id.not_in(keep_ids),
                    )
                )
                deleted_by_volume += result.rowcount or 0

        row = await session.get(AppSetting, SETTING_KEY)
        bookkeeping = dict(policy)
        bookkeeping["last_purge_at"] = datetime.now(timezone.utc).isoformat()
        bookkeeping["last_purge_deleted"] = deleted_by_age + deleted_by_volume
        if row is None:
            row = AppSetting(key=SETTING_KEY, value=bookkeeping)
            session.add(row)
        else:
            row.value = bookkeeping
        await session.commit()  # explicit — purge may run outside a request

    logger.info(
        "retention purge: %s by age, %s by volume (days=%s cap=%s)",
        deleted_by_age, deleted_by_volume, days, cap,
    )
    return {
        "deleted_by_age": deleted_by_age,
        "deleted_by_volume": deleted_by_volume,
        "total": deleted_by_age + deleted_by_volume,
    }
