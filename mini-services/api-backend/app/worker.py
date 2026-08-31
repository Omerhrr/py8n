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

    from .executor import execute_workflow

    # Fresh event loop per task - the async engine + aiosqlite/asyncpg live here.
    result = asyncio.run(
        execute_workflow(
            workflow_id,
            trigger_type=trigger_type,
            trigger_payload=trigger_payload,
            trigger_node_id=trigger_node_id,
            execution_id=execution_id,
        )
    )
    return {"status": result["status"], "execution_id": result["execution_id"]}
