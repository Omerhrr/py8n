"""Execution service - runs the GraphRunner and persists ExecutionLogs."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import AsyncSessionLocal
from ..engine.runner import GraphRunner, validate_graph_document
from ..models import ExecutionLog, Workflow
from .events import get_event_bus

# Keep references to in-flight tasks so asyncio GC doesn't cancel them.
_background_tasks: set = set()
# Cooperative cancellation: execution_id -> Event the runner checks between nodes.
_cancel_flags: dict[str, Any] = {}
# Hard cancellation: execution_id -> asyncio.Task (task.cancel() aborts at the
# next await point, e.g. a long delay node - no waiting for it to finish).
_running_tasks: dict[str, Any] = {}
# v38 live progress for the queue view: execution_id -> {workflow_id,
# workflow_name, trigger_type, started_at, nodes_total, nodes_done,
# current_node, status}. Fed by the emit wrapper below, popped when the run
# leaves the running state. Lost on restart (the queue endpoint falls back to
# the DB rows for anything not tracked here).
_live_progress: dict[str, dict] = {}


def _track_progress(execution_id: str, event: dict) -> None:
    """Update the in-memory progress snapshot from runner events (best effort)."""
    prog = _live_progress.get(execution_id)
    if prog is None:
        return
    kind = event.get("event")
    if kind == "node_started":
        prog["current_node"] = event.get("node_name")
    elif kind == "node_finished":
        prog["nodes_done"] = prog.get("nodes_done", 0) + 1
        prog["current_node"] = None
    elif kind == "execution_finished":
        prog["status"] = event.get("status", "finished")


async def execute_workflow(
    workflow_id: str,
    trigger_type: str = "manual",
    trigger_payload: dict | None = None,
    trigger_node_id: str | None = None,
    execution_id: str | None = None,
    log_created: bool = False,
    respond_channel: Any | None = None,
) -> dict:
    """Load the workflow, run the graph, persist and broadcast events."""
    execution_id = execution_id or uuid.uuid4().hex

    async with AsyncSessionLocal() as session:
        workflow: Workflow | None = (
            await session.execute(select(Workflow).where(Workflow.id == workflow_id))
        ).scalar_one_or_none()
    if workflow is None:
        raise LookupError(f"Workflow {workflow_id} not found")

    graph = validate_graph_document(workflow.graph or {"nodes": [], "edges": []})
    if not graph.trigger_nodes():
        raise ValueError("Workflow has no trigger node - add a trigger to run it")

    bus = get_event_bus()
    if not log_created:
        log = ExecutionLog(
            id=execution_id,
            workflow_id=workflow_id,
            status="running",
            trigger_type=trigger_type,
            trigger_payload=trigger_payload or {},
        )
        async with AsyncSessionLocal() as session:
            session.add(log)
            await session.commit()

    async def emit(event: dict) -> None:
        _track_progress(execution_id, event)
        await bus.publish(execution_id, event)

    cancel_event = _cancel_flags[execution_id] = asyncio.Event()
    _live_progress[execution_id] = {
        "workflow_id": workflow_id,
        "workflow_name": workflow.name,
        "trigger_type": trigger_type,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "nodes_total": len(graph.nodes),
        "nodes_done": 0,
        "current_node": None,
        "status": "running",
    }
    runner = GraphRunner(
        graph,
        workflow_id=workflow_id,
        workflow_name=workflow.name,
        trigger_type=trigger_type,
        trigger_payload=trigger_payload or {},
        trigger_node_id=trigger_node_id,
        emit=emit,
        max_output_capture=settings.max_output_capture,
        execution_id=execution_id,
        cancel_event=cancel_event,
        # v17: only manual runs honor pinned node data - webhook / schedule /
        # error dispatches always execute for real.
        honor_pinned=(trigger_type == "manual"),
        respond_channel=respond_channel,
    )
    try:
        result = await runner.run()
    except asyncio.CancelledError:
        # Hard cancel: the runner was aborted at an await point. Finalise what
        # completed so far as 'cancelled' and swallow the cancellation so the
        # background task ends cleanly.
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(ExecutionLog)
                .where(ExecutionLog.id == execution_id)
                .values(
                    status="cancelled",
                    error="Cancelled by user",
                    finished_at=datetime.now(timezone.utc),
                    node_runs=runner.node_runs,
                )
            )
            await session.commit()
        return {
            "execution_id": execution_id,
            "status": "cancelled",
            "error": "Cancelled by user",
            "duration_ms": None,
            "node_runs": runner.node_runs,
            "context": {},
        }
    finally:
        _cancel_flags.pop(execution_id, None)
        _live_progress.pop(execution_id, None)

    waiting = result["status"] == "waiting"
    async with AsyncSessionLocal() as session:
        snapshot = dict(result["context"] or {})
        if waiting:
            # Persist everything the resume call needs to rebuild the run:
            # node states, active edges, paused node + token.
            snapshot["py8n_resume"] = {
                "token": result["resume"]["token"],
                "node_id": result["resume"]["node_id"],
                "node_states": result["resume_state"]["node_states"],
                "active_edges": result["resume_state"]["active_edges"],
            }
        await session.execute(
            update(ExecutionLog)
            .where(ExecutionLog.id == execution_id)
            .values(
                status=result["status"],
                error=result["error"],
                finished_at=None if waiting else datetime.now(timezone.utc),
                duration_ms=result["duration_ms"],
                node_runs=result["node_runs"],
                context_snapshot=snapshot,
            )
        )
        await _trim_history(session, workflow_id)
        await session.commit()

    # ------------------------------------------------------------
    # Error-workflow routing: an unhandled failure can dispatch a
    # dedicated handler workflow with a structured error payload
    # (n8n "Error workflow"). Guards: never re-route an error-handler
    # run (trigger_type == "error") and never bind a workflow to itself.
    # ------------------------------------------------------------
    if result["status"] == "error" and trigger_type != "error":
        handler_id = workflow.error_workflow_id
        if handler_id and handler_id != workflow_id:
            async with AsyncSessionLocal() as session:
                handler_exists = (
                    await session.execute(select(Workflow.id).where(Workflow.id == handler_id))
                ).scalar_one_or_none()
            if handler_exists:
                failed = [
                    {"node_id": r.get("node_id"), "node_name": r.get("node_name"), "error": r.get("error")}
                    for r in result.get("node_runs") or []
                    if r.get("status") == "error" and not r.get("continued_on_fail")
                ]
                result["error_workflow_execution_id"] = await dispatch_inline(
                    handler_id,
                    trigger_type="error",
                    trigger_payload={
                        "execution_id": execution_id,
                        "workflow_id": workflow_id,
                        "workflow_name": workflow.name,
                        "error": result.get("error"),
                        "failed_nodes": failed,
                    },
                )

    return result


async def resume_workflow(execution_id: str, token: str, payload: Any = None) -> dict:
    """Continue a suspended (Wait for Resume) execution.

    Validates the token, rebuilds a GraphRunner from the persisted state and
    finishes the run in a background task (the execution row is updated in
    place - same execution id, n8n-style continuation).
    """
    import json as _json

    from sqlalchemy import select

    from .events import get_event_bus

    async with AsyncSessionLocal() as session:
        log = await session.get(ExecutionLog, execution_id)
        if log is None:
            raise LookupError(f"Execution {execution_id} not found")
        if log.status != "waiting":
            raise ValueError(f"Execution {execution_id} is not waiting (status={log.status})")
        resume_meta = (log.context_snapshot or {}).get("py8n_resume") or {}
        if not resume_meta or not resume_meta.get("token"):
            raise PermissionError("Execution has no resume state")
        if token != resume_meta["token"]:
            raise PermissionError("Invalid resume token")
        workflow_row = (
            await session.execute(select(Workflow).where(Workflow.id == log.workflow_id))
        ).scalar_one_or_none()
        prior_runs = _json.loads(_json.dumps(log.node_runs or []))  # deep copy
        prior_duration = log.duration_ms or 0

    if workflow_row is None:
        raise LookupError(f"Workflow {log.workflow_id} no longer exists")

    from ..engine.runner import GraphRunner, validate_graph_document

    graph = validate_graph_document(workflow_row.graph or {"nodes": [], "edges": []})

    wait_output = payload if isinstance(payload, dict) else {"payload": payload}
    bus = get_event_bus()

    async def emit(event: dict) -> None:
        await bus.publish(execution_id, event)

    runner = GraphRunner(
        graph,
        workflow_id=workflow_row.id,
        workflow_name=workflow_row.name,
        trigger_type=log.trigger_type or "manual",
        trigger_payload=log.trigger_payload or {},
        execution_id=execution_id,
        emit=emit,
        max_output_capture=settings.max_output_capture,
        resume_state={
            "node_states": resume_meta.get("node_states") or {},
            "active_edges": resume_meta.get("active_edges") or [],
            "wait_node_id": resume_meta.get("node_id"),
            "wait_output": wait_output,
            "prior_node_runs": prior_runs,
        },
    )

    # Flip to running synchronously so the 202 response guarantees pollers see
    # "running" (never the stale "waiting") from this point on.
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(ExecutionLog)
            .where(ExecutionLog.id == execution_id)
            .values(status="running", finished_at=None)
        )
        await session.commit()

    async def _finish() -> None:
        result = await runner.run()
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(ExecutionLog)
                .where(ExecutionLog.id == execution_id)
                .values(
                    status=result["status"],
                    error=result["error"],
                    finished_at=datetime.now(timezone.utc),
                    # total wall time = first pass + resume segment
                    duration_ms=prior_duration + result["duration_ms"],
                    node_runs=result["node_runs"],
                    # drop py8n_resume → token invalidated after resume
                    context_snapshot=dict(result["context"] or {}),
                )
            )
            await session.commit()

    task = asyncio_create_task(_finish())
    _background_tasks.add(task)
    _running_tasks[execution_id] = task

    def _discard_resume(t, *, _id=execution_id):
        _background_tasks.discard(t)
        _running_tasks.pop(_id, None)

    task.add_done_callback(_discard_resume)
    return {
        "execution_id": execution_id,
        "workflow_id": workflow_row.id,
        "resume_node": resume_meta.get("node_id"),
        "status": "resuming",
    }


async def _trim_history(session: AsyncSession, workflow_id: str) -> None:
    """Keep only the most recent N execution logs per workflow."""
    ids = (
        await session.execute(
            select(ExecutionLog.id)
            .where(ExecutionLog.workflow_id == workflow_id)
            .order_by(ExecutionLog.started_at.desc())
            .offset(settings.execution_history_limit)
        )
    ).scalars().all()
    if ids:
        await session.execute(delete(ExecutionLog).where(ExecutionLog.id.in_(ids)))


async def dispatch_inline(
    workflow_id: str,
    trigger_type: str = "manual",
    trigger_payload: dict | None = None,
    trigger_node_id: str | None = None,
    execution_id: str | None = None,
) -> str:
    """Fire an execution as a background task on the current event loop."""
    from ..models import ExecutionLog

    exec_id = execution_id or uuid.uuid4().hex
    # Insert the running row up-front so pollers never see a 404 window.
    async with AsyncSessionLocal() as session:
        session.add(
            ExecutionLog(
                id=exec_id,
                workflow_id=workflow_id,
                status="running",
                trigger_type=trigger_type,
                trigger_payload=trigger_payload or {},
            )
        )
        await session.commit()
    task = asyncio_create_task(
        execute_workflow(
            workflow_id, trigger_type, trigger_payload, trigger_node_id, exec_id, log_created=True
        )
    )
    _background_tasks.add(task)
    _running_tasks[exec_id] = task

    def _discard(t, *, _id=exec_id):
        _background_tasks.discard(t)
        _running_tasks.pop(_id, None)

    task.add_done_callback(_discard)
    return exec_id


def asyncio_create_task(coro):
    import asyncio

    return asyncio.get_running_loop().create_task(coro)


async def cancel_execution(execution_id: str) -> dict:
    """Cancel a running execution.

    Two mechanisms:
    * in-flight background task (dispatch/resume) → task.cancel() aborts the
      runner at its next await point, which finalises the row as cancelled;
    * direct (inline) execute_workflow calls → the cooperative Event the
      runner checks before each node.
    Idempotent while winding down; unknown/finished executions → 404 / 409.
    """
    task = _running_tasks.get(execution_id)
    event = _cancel_flags.get(execution_id)
    if task is None and event is None:
        async with AsyncSessionLocal() as session:
            log = await session.get(ExecutionLog, execution_id)
            if log is None:
                raise LookupError(f"Execution {execution_id} not found")
            raise ValueError(
                f"Execution {execution_id} is not running (status={log.status}) - nothing to cancel"
            )
    if task is not None:
        task.cancel()
    if event is not None:
        event.set()
    return {"execution_id": execution_id, "status": "cancelling"}
