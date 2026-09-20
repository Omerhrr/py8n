"""Celery worker (Phase 5, production mode).

Enabled by setting PY8N_EXECUTION_MODE=celery and PY8N_REDIS_URL. The worker
executes the same GraphRunner as inline mode, publishing events to the Redis
event bus so any API replica can stream progress to WebSockets.

Run:  celery -A app.worker worker --loglevel=info --concurrency=4
"""

from __future__ import annotations

import asyncio

from celery import Celery

from .config import settings

celery_app = Celery(
    "py8n",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    worker_prefetch_multiplier=1,  # long-running workflows: fair dispatch
    task_soft_time_limit=3600,
    task_time_limit=3900,
)


def _sync_database_url() -> str:
    """Derive a sync DB URL for quick worker-side lookups."""
    url = settings.database_url
    return url.replace("+aiosqlite", "").replace("+asyncpg", "")


async def _run_and_cleanup(workflow_id: str, trigger_type: str, trigger_payload: dict,
                           trigger_node_id: str | None, execution_id: str) -> dict:
    """Run the workflow, then dispose every per-loop async resource BEFORE
    this task's asyncio.run() closes its event loop.

    v111: app.db's async SQLAlchemy engine and app.services.events' cached
    RedisEventBus are both process-wide singletons, built once and reused
    for the life of the worker process - fine for the API's one long-lived
    loop, but this task gets a BRAND NEW event loop every single time
    (asyncio.run() per task). Their pooled connections stay bound to
    whichever loop was running when first opened, so any task after the
    first one to touch either resource raised `Future ... attached to a
    different loop` (seen live on a webhook-triggered run: the manual Run
    right after a fresh worker boot succeeded because it was the first
    task on that process; the next task - a different trigger, same
    worker - hit the stale connections). Disposing/resetting them here,
    still inside THIS task's loop, means the next task starts clean
    instead of inheriting a dead connection tied to a closed loop.
    """
    from .db import engine as db_engine
    from .services.events import reset_event_bus
    from .services.executor import execute_workflow

    try:
        return await execute_workflow(
            workflow_id,
            trigger_type=trigger_type,
            trigger_payload=trigger_payload,
            trigger_node_id=trigger_node_id,
            execution_id=execution_id,
            log_created=True,  # dispatcher pre-created the running row
        )
    finally:
        try:
            await db_engine.dispose()
        except Exception:  # noqa: BLE001 - cleanup must never mask the task's own result
            pass
        await reset_event_bus()


@celery_app.task(name="py8n.execute_workflow", bind=True, max_retries=1)
def execute_workflow_task(self, workflow_id: str, trigger_type: str, trigger_payload: dict,
                          trigger_node_id: str | None, execution_id: str) -> dict:
    """Fetch the workflow synchronously, then run the async engine to completion."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from .models import Workflow

    engine = create_engine(_sync_database_url())
    try:
        with Session(engine) as session:
            workflow = session.get(Workflow, workflow_id)
            if workflow is None:
                raise LookupError(f"Workflow {workflow_id} not found")
            graph_doc = workflow.graph or {}
            workflow_name = workflow.name
    finally:
        engine.dispose()

    # Bug fix: this was `from .executor import execute_workflow` - wrong
    # path (worker.py lives in app/, but execute_workflow lives in
    # app/services/executor.py), so it raised ModuleNotFoundError on EVERY
    # single task, for every workflow, since the stack was first stood up.
    # Worse, the import sat OUTSIDE the try/except below that is supposed
    # to finalize a crashed task as 'failed' - so the ImportError killed
    # the task with NO cleanup at all, leaving the dispatcher's pre-created
    # 'running' row orphaned forever. That is exactly the "stuck at 0/?
    # nodes done for hours" symptom seen on /executions for every
    # scheduled and manually-run workflow. Moving the import INSIDE the
    # try block (not just fixing the path) means any future import-time
    # break here still gets caught and finalized instead of silently
    # orphaning the row again.
    try:
        # Fresh event loop per task - the async engine + aiosqlite/asyncpg live here.
        result = asyncio.run(_run_and_cleanup(
            workflow_id, trigger_type, trigger_payload, trigger_node_id, execution_id,
        ))
    except Exception as exc:
        # The dispatcher pre-creates the running row; if the task crashes
        # before/inside the async engine, that row must NOT stay 'running'
        # forever - finalize it as a failed execution, then let Celery see
        # the failure.
        _mark_execution_failed(execution_id, exc)
        raise
    return {"status": result["status"], "execution_id": result["execution_id"]}


def _mark_execution_failed(execution_id: str, exc: Exception) -> None:
    """Best-effort: stamp the execution row as failed with the error message."""
    import logging

    from sqlalchemy import create_engine, select, update
    from sqlalchemy.orm import Session

    from .models import ExecutionLog

    logger = logging.getLogger("py8n.worker")
    try:
        engine = create_engine(_sync_database_url())
        try:
            with Session(engine) as session:
                row = session.execute(
                    select(ExecutionLog.id).where(ExecutionLog.id == execution_id)
                ).scalar_one_or_none()
                if row is None:
                    session.add(
                        ExecutionLog(
                            id=execution_id,
                            workflow_id="",
                            status="error",
                            trigger_type="manual",
                            error=f"Unhandled execution failure: {exc}"[:5000],
                        )
                    )
                else:
                    session.execute(
                        update(ExecutionLog)
                        .where(ExecutionLog.id == execution_id)
                        .values(
                            status="error",
                            error=f"Unhandled execution failure: {exc}"[:5000],
                        )
                    )
                session.commit()
        finally:
            engine.dispose()
    except Exception:  # noqa: BLE001 - the safety net must never mask the error
        logger.exception("failed to mark execution %s as failed", execution_id)
