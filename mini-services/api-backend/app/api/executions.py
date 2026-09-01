"""Execution history endpoints + live run retrieval + rerun."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user, own_or_404
from ..db import get_db
from ..models import ExecutionLog, Workflow
from ..schemas import ResumeRequest
from ..services.executor import (  # noqa: F401 (rerun + cancel)
    cancel_execution,
    dispatch_inline,
    resume_workflow,
)

router = APIRouter(prefix="/executions", tags=["executions"])


async def _own_workflow_or_404(db: AsyncSession, workflow_id: str, user) -> None:
    """v37: an execution of another user's workflow looks nonexistent."""
    wf = await db.get(Workflow, workflow_id)
    own_or_404(wf.owner_id if wf else None, user)


@router.get("")
async def list_executions(
    workflow_id: str | None = Query(default=None),
    status: str | None = Query(default=None, description="success | error | running | waiting | cancelled"),
    limit: int = Query(default=25, ge=1, le=100),
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(ExecutionLog).order_by(ExecutionLog.started_at.desc()).limit(limit)
    if workflow_id:
        stmt = stmt.where(ExecutionLog.workflow_id == workflow_id)
    if status:
        stmt = stmt.where(ExecutionLog.status == status)
    if user is not None:
        # v37: scope to executions of unclaimed or own workflows
        from ..auth import visible_workflow_ids

        visible = await visible_workflow_ids(db, user)
        stmt = stmt.where(ExecutionLog.workflow_id.in_(visible))
    rows = (await db.execute(stmt)).scalars().all()

    # Batch-resolve workflow names (single extra query, avoids N+1).
    names: dict[str, str] = {}
    wf_ids = {r.workflow_id for r in rows}
    if wf_ids:
        name_rows = (
            await db.execute(select(Workflow.id, Workflow.name).where(Workflow.id.in_(wf_ids)))
        ).all()
        names = dict(name_rows)

    return [
        {
            "id": r.id,
            "workflow_id": r.workflow_id,
            "workflow_name": names.get(r.workflow_id),
            "status": r.status,
            "trigger_type": r.trigger_type,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "duration_ms": r.duration_ms,
            "error": (r.error[:300] if r.error else None),
        }
        for r in rows
    ]


@router.get("/queue")
async def execution_queue(
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """v38: the live queue - every running / waiting execution with progress.

    Merges the DB rows (source of truth, survives restarts) with the
    executor's in-memory progress map (nodes done / total, current node).
    Progress fields are null for rows the map no longer tracks (e.g. resumed
    runs after a restart). Ordered oldest first: the queue is FIFO-ish.
    """
    from datetime import datetime

    from ..auth import visible_workflow_ids
    from ..services.executor import _live_progress

    stmt = (
        select(ExecutionLog)
        .where(ExecutionLog.status.in_(["running", "waiting"]))
        .order_by(ExecutionLog.started_at.asc())
        .limit(100)
    )
    if user is not None:
        visible = await visible_workflow_ids(db, user)
        stmt = stmt.where(ExecutionLog.workflow_id.in_(visible))
    rows = (await db.execute(stmt)).scalars().all()

    names: dict[str, str] = {}
    wf_ids = {r.workflow_id for r in rows}
    if wf_ids:
        name_rows = (
            await db.execute(select(Workflow.id, Workflow.name).where(Workflow.id.in_(wf_ids)))
        ).all()
        names = dict(name_rows)

    now = datetime.utcnow()  # started_at is stored naive (sqlite) - stay naive
    items = []
    for r in rows:
        prog = _live_progress.get(r.id) or {}
        nodes_total = prog.get("nodes_total")
        nodes_done = prog.get("nodes_done", 0)
        if nodes_total:
            nodes_done = min(nodes_done, nodes_total)
        started = r.started_at
        items.append(
            {
                "execution_id": r.id,
                "workflow_id": r.workflow_id,
                "workflow_name": names.get(r.workflow_id),
                "trigger_type": r.trigger_type,
                "status": r.status,
                "started_at": started.isoformat() if started else None,
                "duration_ms": (
                    int((now - started).total_seconds() * 1000)
                    if started and r.status == "running"
                    else r.duration_ms
                ),
                "nodes_done": nodes_done,
                "nodes_total": nodes_total,
                "current_node": prog.get("current_node"),
            }
        )
    return {"items": items, "total": len(items)}


@router.get("/{execution_id}")
async def get_execution(execution_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    r = await db.get(ExecutionLog, execution_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Execution not found")
    await _own_workflow_or_404(db, r.workflow_id, user)  # v37
    body = {
        "id": r.id,
        "workflow_id": r.workflow_id,
        "status": r.status,
        "trigger_type": r.trigger_type,
        "trigger_payload": r.trigger_payload,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "duration_ms": r.duration_ms,
        "node_runs": r.node_runs or [],
        "error": r.error,
    }
    if r.status == "waiting":
        meta = (r.context_snapshot or {}).get("py8n_resume") or {}
        if meta.get("token"):
            body["resume"] = {
                "method": "POST",
                "url": f"/executions/{r.id}/resume",
                "token": meta["token"],
                "node_id": meta.get("node_id"),
            }
    return body


@router.post("/{execution_id}/resume", status_code=202)
async def resume_execution(execution_id: str, body: ResumeRequest, db: AsyncSession = Depends(get_db)):
    """Continue a suspended Wait-for-Resume execution with the given token.

    The resume payload becomes the wait node's output; the SAME execution id
    continues (status flips back to running, then success/error).
    """
    try:
        result = await resume_workflow(execution_id, body.token, body.payload)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result


@router.post("/{execution_id}/cancel", status_code=202)
async def cancel_execution_endpoint(execution_id: str):
    """Cooperatively cancel a running execution (runner stops between nodes).

    The execution row flips to ``cancelled`` synchronously; the in-flight
    background task winds down at the next node boundary.
    """
    try:
        return await cancel_execution(execution_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{execution_id}/rerun", status_code=202)
async def rerun_execution(execution_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Re-execute the workflow with the recorded trigger payload (n8n-style retry).

    Runs against the *current* workflow graph; returns the new execution id.
    """
    src = await db.get(ExecutionLog, execution_id)
    if src is None:
        raise HTTPException(status_code=404, detail="Execution not found")
    workflow = await db.get(Workflow, src.workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="Source workflow no longer exists")
    own_or_404(workflow.owner_id, user)  # v37
    new_id = await dispatch_inline(
        src.workflow_id,
        trigger_type=src.trigger_type or "manual",
        trigger_payload=src.trigger_payload or {},
    )
    return {"execution_id": new_id, "rerun_of": execution_id, "workflow_id": src.workflow_id}


@router.delete("/{execution_id}")
async def delete_execution(execution_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    r = await db.get(ExecutionLog, execution_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Execution not found")
    await _own_workflow_or_404(db, r.workflow_id, user)  # v37
    await db.delete(r)
    # Commit inside the endpoint: the get_db dependency's teardown commit runs
    # AFTER the response is sent, which would let an immediate follow-up GET
    # observe the not-yet-committed deletion.
    await db.commit()
    return {"ok": True, "id": execution_id}
