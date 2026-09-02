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
  (active executions are never touched);
* volume-based: per workflow, only the newest ``max_executions_per_workflow``
  finished executions survive;
* orphan sweep (v44): artifact rows whose execution no longer exists (usually
  because the run was just purged) are deleted together with their files -
  before this sweep purged executions leaked their chart/model files forever.
  The sweep ALSO scans the artifacts directory for files with no DB row
  (crash leftovers, rows removed elsewhere) and removes those too, so the
  on-disk footprint can only shrink.

Active executions (status ``running`` or ``waiting`` - webhook wait / human
resume wait) are NEVER deleted; deleting a waiting row would orphan its
resume token and strand the run forever.

Explicit commits everywhere - the yield-dependency teardown commit runs after
the response is sent, so background/purge writes must commit themselves.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, select

from ..config import settings
from ..db import AsyncSessionLocal
from ..models import AppSetting, Artifact, ExecutionLog

logger = logging.getLogger("py8n.retention")

SETTING_KEY = "execution_retention"

# Statuses that must NEVER be purged: a running row is self-explanatory; a
# waiting row is suspended on a Wait-for-Resume / webhook-wait node and still
# holds a live resume token in its context snapshot.
ACTIVE_STATUSES = ("running", "waiting")

# Disk-orphan sweep: files younger than this are skipped so a concurrent
# save_artifact (row flushed, file written, commit pending) is never deleted.
ORPHAN_FILE_GRACE_SECONDS = 3600


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
        await session.commit()  # explicit - teardown commit is too late
    return current


async def purge_execution_data() -> dict:
    """Apply the retention policy. Returns {deleted_by_age, deleted_by_volume,
    artifacts_deleted, orphan_files_deleted, total}.

    v20: workflows may override the global age policy via
    ``Workflow.retention_days`` (NULL = inherit, 0 = keep forever, N = days).
    The global volume cap stays uniform across all workflows.
    """
    from ..models import Workflow

    policy = await get_policy()
    days = policy["retention_days"]
    cap = policy["max_executions_per_workflow"]
    deleted_by_age = 0
    deleted_by_volume = 0
    deleted_artifacts = 0
    deleted_orphan_files = 0

    async with AsyncSessionLocal() as session:
        override_rows = (
            await session.execute(
                select(Workflow.id, Workflow.retention_days).where(Workflow.retention_days.is_not(None))
            )
        ).all()
        overridden_ids = [row[0] for row in override_rows]

        if days > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            conditions = [
                ExecutionLog.status.not_in(ACTIVE_STATUSES),
                ExecutionLog.finished_at.is_not(None),
                ExecutionLog.finished_at < cutoff,
            ]
            if overridden_ids:
                conditions.append(ExecutionLog.workflow_id.not_in(overridden_ids))
            result = await session.execute(delete(ExecutionLog).where(*conditions))
            deleted_by_age += result.rowcount or 0

        # per-workflow overrides (0 = keep forever -> skipped here)
        for workflow_id, override_days in override_rows:
            if override_days <= 0:
                continue
            cutoff_w = datetime.now(timezone.utc) - timedelta(days=override_days)
            result = await session.execute(
                delete(ExecutionLog).where(
                    ExecutionLog.workflow_id == workflow_id,
                    ExecutionLog.status.not_in(ACTIVE_STATUSES),
                    ExecutionLog.finished_at.is_not(None),
                    ExecutionLog.finished_at < cutoff_w,
                )
            )
            deleted_by_age += result.rowcount or 0

        if cap > 0:
            workflow_ids = (
                await session.execute(select(ExecutionLog.workflow_id).distinct())
            ).scalars().all()
            for workflow_id in workflow_ids:
                keep_ids = (
                    await session.execute(
                        select(ExecutionLog.id)
                        .where(
                            ExecutionLog.workflow_id == workflow_id,
                            ExecutionLog.status.not_in(ACTIVE_STATUSES),
                        )
                        .order_by(ExecutionLog.started_at.desc())
                        .limit(cap)
                    )
                ).scalars().all()
                if len(keep_ids) < cap:
                    continue  # under the cap - nothing to do
                result = await session.execute(
                    delete(ExecutionLog).where(
                        ExecutionLog.workflow_id == workflow_id,
                        ExecutionLog.status.not_in(ACTIVE_STATUSES),
                        ExecutionLog.id.not_in(keep_ids),
                    )
                )
                deleted_by_volume += result.rowcount or 0

        # v44/audit: orphan artifact sweep. Runs UNCONDITIONALLY (not only when
        # this purge deleted rows) because other paths (e.g. history trimming)
        # can orphan artifacts too.
        # 1) DB rows whose execution no longer exists -> delete row + file.
        from . import artifacts as art_svc

        orphan_rows = (
            await session.execute(
                select(Artifact).where(
                    Artifact.execution_id.is_not(None),
                    Artifact.execution_id.not_in(select(ExecutionLog.id)),
                )
            )
        ).scalars().all()
        for art in orphan_rows:
            art_svc.delete_file(art)
            await session.delete(art)
            deleted_artifacts += 1

        # 2) Files on disk with no artifact row (crash between write/commit,
        #    rows deleted elsewhere) -> unlink the file. Recent files are
        #    skipped so an in-flight save is never swept.
        known_ids = set((await session.execute(select(Artifact.id))).scalars().all())
        artifacts_root = Path(settings.artifacts_dir)
        if artifacts_root.exists():
            now = time.time()
            for f in artifacts_root.iterdir():
                if not f.is_file():
                    continue
                if f.stem in known_ids:
                    continue
                try:
                    if now - f.stat().st_mtime < ORPHAN_FILE_GRACE_SECONDS:
                        continue
                    f.unlink()
                    deleted_orphan_files += 1
                except OSError:
                    logger.warning("could not sweep orphan artifact file %s", f, exc_info=True)

        row = await session.get(AppSetting, SETTING_KEY)
        bookkeeping = dict(policy)
        bookkeeping["last_purge_at"] = datetime.now(timezone.utc).isoformat()
        bookkeeping["last_purge_deleted"] = deleted_by_age + deleted_by_volume
        bookkeeping["last_purge_artifacts"] = deleted_artifacts
        bookkeeping["last_purge_orphan_files"] = deleted_orphan_files
        if row is None:
            row = AppSetting(key=SETTING_KEY, value=bookkeeping)
            session.add(row)
        else:
            row.value = bookkeeping
        await session.commit()  # explicit - purge may run outside a request

    logger.info(
        "retention purge: %s by age, %s by volume, %s orphan artifacts, %s orphan files (days=%s cap=%s)",
        deleted_by_age, deleted_by_volume, deleted_artifacts, deleted_orphan_files, days, cap,
    )
    return {
        "deleted_by_age": deleted_by_age,
        "deleted_by_volume": deleted_by_volume,
        "artifacts_deleted": deleted_artifacts,
        "orphan_files_deleted": deleted_orphan_files,
        "total": deleted_by_age + deleted_by_volume,
    }
