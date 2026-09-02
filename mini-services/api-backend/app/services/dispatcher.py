"""Execution dispatcher - picks inline (sandbox) or Celery (production) transport.

Both paths share the same contract: return an execution id immediately; the
actual run happens in the background and streams events to the event bus.

Both paths also pre-create the ``running`` ExecutionLog row, so the id the
caller receives resolves immediately (no 404 window) and a crashed dispatch
can always be finalized as failed.
"""

from __future__ import annotations

import uuid

from ..config import settings


async def dispatch_execution(
    workflow_id: str,
    trigger_type: str = "manual",
    trigger_payload: dict | None = None,
    trigger_node_id: str | None = None,
) -> str:
    """Queue a workflow execution and return its execution id (202-style)."""
    execution_id = uuid.uuid4().hex

    if settings.execution_mode == "celery":
        # Pre-create the running row (same contract as dispatch_inline) so the
        # returned id resolves immediately and a crashed dispatch can always
        # be finalized as failed by the worker.
        from ..models import ExecutionLog
        from ..db import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            session.add(
                ExecutionLog(
                    id=execution_id,
                    workflow_id=workflow_id,
                    status="running",
                    trigger_type=trigger_type,
                    trigger_payload=trigger_payload or {},
                )
            )
            await session.commit()

        # Production: hand off to a distributed Celery worker via Redis.
        from ..worker import execute_workflow_task

        execute_workflow_task.delay(
            workflow_id, trigger_type, trigger_payload or {}, trigger_node_id, execution_id
        )
        return execution_id

    # Sandbox / single-process mode: run inline on the event loop.
    from .executor import dispatch_inline

    return await dispatch_inline(
        workflow_id, trigger_type, trigger_payload, trigger_node_id, execution_id
    )
