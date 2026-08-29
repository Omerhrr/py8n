"""Execution dispatcher — picks inline (sandbox) or Celery (production) transport.

Both paths share the same contract: return an execution id immediately; the
actual run happens in the background and streams events to the event bus.
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
