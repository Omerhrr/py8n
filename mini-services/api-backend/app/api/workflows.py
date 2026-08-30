"""Workflow CRUD + manual run + webhook URL (Phase 2 core API)."""

from __future__ import annotations

import time
import uuid
from collections import Counter

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..engine.context import ExecutionContext
from ..engine.nodes.base import NodeExecutionError
from ..engine.runner import GraphValidationError, validate_graph_document
from ..models import Folder, Workflow, WorkflowVersion
from ..schemas import (
    NodeTestRequest,
    RunAccepted,
    RunRequest,
    WorkflowCreate,
    WorkflowExportDoc,
    WorkflowImportRequest,
    WorkflowListItem,
    WorkflowOut,
    WorkflowScheduleOut,
    WorkflowUpdate,
)
from ..services.dispatcher import dispatch_execution
from ..services.scheduler import resync_workflow_jobs, schedule_entries_for_graph, validate_schedule_params
from ..services.versions import MAX_VERSIONS, snapshot_workflow_version

router = APIRouter(prefix="/workflows", tags=["workflows"])


def _trigger_types(graph: dict) -> list[str]:
    return sorted({n.get("type", "") for n in (graph or {}).get("nodes", []) if n.get("type", "").endswith("_trigger")})


def _validate_schedule_nodes(graph: dict) -> None:
    """Reject unschedulable schedule nodes at save time (silent no-fire before v7)."""
    for node in (graph or {}).get("nodes", []):
        if node.get("type") != "schedule_trigger":
            continue
        params = node.get("parameters") or {}
        try:
            validate_schedule_params(params)
        except (ValueError, TypeError) as exc:
            label = node.get("name") or node.get("id") or "Schedule Trigger"
            raise HTTPException(status_code=400, detail=f"Schedule node '{label}': {exc}") from exc


def _next_run_at(graph: dict, is_active: bool) -> str | None:
    """Soonest upcoming fire time across the workflow's schedule nodes."""
    if not is_active:
        return None
    runs = [e["next_runs"][0] for e in schedule_entries_for_graph(graph) if e["next_runs"]]
    return min(runs) if runs else None


def _schedule_out(wf: Workflow) -> WorkflowScheduleOut:
    entries = schedule_entries_for_graph(wf.graph or {})
    return WorkflowScheduleOut(
        workflow_id=wf.id,
        is_active=wf.is_active,
        schedules=entries,
        next_run_at=_next_run_at(wf.graph or {}, wf.is_active),
    )


@router.get("", response_model=list[WorkflowListItem])
async def list_workflows(
    tag: str | None = Query(default=None, description="Filter: workflows carrying this tag"),
    search: str | None = Query(default=None, description="Case-insensitive substring on name/description"),
    folder_id: str | None = Query(default=None, description="Filter: folder id, or 'none' for unfiled"),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(select(Workflow).order_by(Workflow.updated_at.desc()))).scalars().all()
    # Tag / search / folder filters run in Python — the gallery is small and
    # the JSON columns keep this portable across SQLite and PostgreSQL.
    if tag:
        want = tag.strip().lower()
        rows = [w for w in rows if want in (t.lower() for t in (w.tags or []))]
    if search:
        needle = search.strip().lower()
        if needle:
            rows = [
                w for w in rows
                if needle in w.name.lower() or needle in (w.description or "").lower()
            ]
    if folder_id:
        if folder_id == "none":
            rows = [w for w in rows if not w.folder_id]
        else:
            rows = [w for w in rows if w.folder_id == folder_id]
    # Batch-resolve error-workflow names (single extra query, avoids N+1).
    handler_ids = {w.error_workflow_id for w in rows if w.error_workflow_id}
    handler_names: dict[str, str] = {}
    if handler_ids:
        name_rows = (
            await db.execute(select(Workflow.id, Workflow.name).where(Workflow.id.in_(handler_ids)))
        ).all()
        handler_names = dict(name_rows)
    # Batch-resolve folder names (v16).
    folder_ids = {w.folder_id for w in rows if w.folder_id}
    folder_names: dict[str, str] = {}
    if folder_ids:
        folder_rows = (
            await db.execute(select(Folder.id, Folder.name).where(Folder.id.in_(folder_ids)))
        ).all()
        folder_names = dict(folder_rows)
    return [
        WorkflowListItem(
            id=w.id,
            name=w.name,
            description=w.description,
            is_active=w.is_active,
            node_count=len((w.graph or {}).get("nodes", [])),
            trigger_types=_trigger_types(w.graph),
            schedule_summary=_schedule_summary(w.graph),
            next_run_at=_next_run_at(w.graph or {}, w.is_active),
            error_workflow_id=w.error_workflow_id,
            error_workflow_name=handler_names.get(w.error_workflow_id) if w.error_workflow_id else None,
            tags=w.tags or [],
            folder_id=w.folder_id,
            folder_name=folder_names.get(w.folder_id) if w.folder_id else None,
            retention_days=w.retention_days,
            created_at=w.created_at,
            updated_at=w.updated_at,
        )
        for w in rows
    ]


@router.get("/tags")
async def tags_summary(db: AsyncSession = Depends(get_db)):
    """Distinct tag vocabulary with usage counts (dashboard filter chips).

    NOTE: declared before /{workflow_id} so "tags" is not eaten as an id.
    """
    rows = (await db.execute(select(Workflow.tags))).scalars().all()
    counts: Counter[str] = Counter()
    for tags in rows:
        for t in tags or []:
            counts[t.lower()] += 1
    return [{"tag": t, "count": n} for t, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def _schedule_summary(graph: dict | None) -> str | None:
    entries = schedule_entries_for_graph(graph)
    return entries[0]["summary"] if entries else None


async def _validate_error_workflow(db: AsyncSession, workflow_id: str, handler_id: str | None) -> None:
    """Error-workflow binding must point at another existing workflow."""
    if not handler_id:
        return
    if handler_id == workflow_id:
        raise HTTPException(status_code=400, detail="A workflow cannot be its own error workflow")
    from sqlalchemy import select as _select

    exists = (
        await db.execute(_select(Workflow.id).where(Workflow.id == handler_id))
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(status_code=400, detail="Error workflow not found")


async def _validate_folder(db: AsyncSession, folder_id: str | None) -> None:
    """Folder assignment must point at an existing folder (v16)."""
    if not folder_id:
        return
    exists = (await db.execute(select(Folder.id).where(Folder.id == folder_id))).scalar_one_or_none()
    if exists is None:
        raise HTTPException(status_code=400, detail="Folder not found")


@router.post("", response_model=WorkflowOut, status_code=201)
async def create_workflow(body: WorkflowCreate, db: AsyncSession = Depends(get_db)):
    try:
        validate_graph_document(body.graph or {"nodes": [], "edges": []})
    except (GraphValidationError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _validate_schedule_nodes(body.graph or {})
    wf = Workflow(
        name=body.name,
        description=body.description,
        graph=body.graph,
        is_active=body.is_active,
        error_workflow_id=body.error_workflow_id,
        tags=body.tags,
        folder_id=body.folder_id,
    )
    db.add(wf)
    await db.flush()
    await db.refresh(wf)
    await _validate_error_workflow(db, wf.id, wf.error_workflow_id)
    await _validate_folder(db, wf.folder_id)
    await snapshot_workflow_version(db, wf)  # v1 — the created state
    # Commit before responding: the get_db teardown commit runs after the
    # response is sent, so an immediate follow-up request (e.g. POST .../run)
    # on another connection could miss the row.
    await db.commit()
    await resync_workflow_jobs(wf.id)  # register schedule triggers right away
    return wf


@router.get("/{workflow_id}", response_model=WorkflowOut)
async def get_workflow(workflow_id: str, db: AsyncSession = Depends(get_db)):
    wf = await db.get(Workflow, workflow_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return wf


@router.put("/{workflow_id}", response_model=WorkflowOut)
async def update_workflow(workflow_id: str, body: WorkflowUpdate, db: AsyncSession = Depends(get_db)):
    wf = await db.get(Workflow, workflow_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if body.graph is not None:
        try:
            wf.graph = validate_graph_document(body.graph).model_dump()
        except (GraphValidationError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _validate_schedule_nodes(body.graph)
    if body.name is not None:
        wf.name = body.name
    if body.description is not None:
        wf.description = body.description
    if body.is_active is not None:
        wf.is_active = body.is_active
    if "error_workflow_id" in body.model_dump(exclude_unset=True):
        handler = body.error_workflow_id or None  # "" clears the binding
        await _validate_error_workflow(db, workflow_id, handler)
        wf.error_workflow_id = handler
    if "tags" in body.model_dump(exclude_unset=True):
        wf.tags = body.tags or []  # omitted = untouched; [] clears all tags
    if "folder_id" in body.model_dump(exclude_unset=True):
        target = body.folder_id or None  # "" moves the workflow to the root
        await _validate_folder(db, target)
        wf.folder_id = target
    if "retention_days" in body.model_dump(exclude_unset=True):
        # v20: null = inherit global policy; 0 = keep forever; N = N days.
        wf.retention_days = body.retention_days
    await db.flush()
    await db.refresh(wf)
    # Content change (graph/name/description) → snapshot the new state.
    # Tags / activation / error binding are organizational, not content —
    # they don't pollute the history.
    if {"graph", "name", "description"} & set(body.model_dump(exclude_unset=True)):
        await snapshot_workflow_version(db, wf)
    await db.commit()  # see create_workflow — avoid teardown-commit race
    await resync_workflow_jobs(workflow_id)  # keep APScheduler in sync with the canvas
    return wf


@router.delete("/{workflow_id}", status_code=204)
async def delete_workflow(workflow_id: str, db: AsyncSession = Depends(get_db)):
    wf = await db.get(Workflow, workflow_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    await db.delete(wf)
    await db.commit()  # avoid teardown-commit race on follow-up requests
    await resync_workflow_jobs(workflow_id)


@router.get("/{workflow_id}/schedule", response_model=WorkflowScheduleOut)
async def get_workflow_schedule(workflow_id: str, db: AsyncSession = Depends(get_db)):
    """Schedule introspection: per-node summary + upcoming fire-time previews."""
    wf = await db.get(Workflow, workflow_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return _schedule_out(wf)


@router.post("/{workflow_id}/activate", response_model=WorkflowScheduleOut)
async def activate_workflow(workflow_id: str, db: AsyncSession = Depends(get_db)):
    """Enable triggers (schedule firing + webhook reception) with pre-flight checks."""
    wf = await db.get(Workflow, workflow_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    entries = schedule_entries_for_graph(wf.graph or {})
    bad = next((e for e in entries if e["error"]), None)
    if bad is not None:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot activate — schedule node '{bad['node_name']}': {bad['error']}",
        )
    wf.is_active = True
    await db.commit()  # avoid teardown-commit race on follow-up requests
    await resync_workflow_jobs(workflow_id)
    return _schedule_out(wf)


@router.post("/{workflow_id}/deactivate", response_model=WorkflowScheduleOut)
async def deactivate_workflow(workflow_id: str, db: AsyncSession = Depends(get_db)):
    """Pause all triggers (schedule jobs removed, webhook returns 409)."""
    wf = await db.get(Workflow, workflow_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    wf.is_active = False
    await db.commit()  # avoid teardown-commit race on follow-up requests
    await resync_workflow_jobs(workflow_id)
    return _schedule_out(wf)


@router.post("/{workflow_id}/run", response_model=RunAccepted)
async def run_workflow(workflow_id: str, body: RunRequest | None = None, db: AsyncSession = Depends(get_db)):
    wf = await db.get(Workflow, workflow_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    body = body or RunRequest()
    try:
        execution_id = await dispatch_execution(
            workflow_id,
            trigger_type="manual",
            trigger_payload={"payload": body.payload or {}, "requested_trigger_node": body.trigger_node_id},
            trigger_node_id=body.trigger_node_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RunAccepted(execution_id=execution_id)


@router.post("/{workflow_id}/nodes/{node_id}/test")
async def test_node_step(
    workflow_id: str,
    node_id: str,
    body: NodeTestRequest | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Run ONE node in isolation with ad-hoc input (v17 test step).

    Nothing is persisted — no execution log, no scheduler touch. If the node
    has pinned data, the pinned output is returned instead (that's exactly
    what a manual run would produce) and ``pinned_used`` is set.
    """
    wf = await db.get(Workflow, workflow_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    try:
        spec = validate_graph_document(wf.graph or {"nodes": [], "edges": []})
    except (GraphValidationError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    node = spec.node_map().get(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"Node {node_id!r} not found in this workflow")
    from ..engine.registry import get_node_class

    cls = get_node_class(node.type)
    if cls is None:
        raise HTTPException(status_code=400, detail=f"Unknown node type {node.type!r}")

    from ..services.env_vars import load_env_map

    context = ExecutionContext(
        workflow_id=wf.id,
        workflow_name=wf.name,
        execution_id=uuid.uuid4().hex,  # ephemeral — never persisted
        trigger_type="manual",
        trigger_payload={},
        env_vars=await load_env_map() or {},
        honor_pinned=True,
    )

    if node.pinned_data is not None:
        return {
            "ok": True,
            "status": "success",
            "node_id": node.id,
            "node_type": node.type,
            "output": node.pinned_data,
            "outputs": {"main": node.pinned_data},
            "error": None,
            "duration_ms": 0,
            "pinned_used": True,
        }

    items = (body or NodeTestRequest()).items
    if items is not None:
        context.current_inputs = {"__test_input__": items}
        context.current_input = items

    started = time.monotonic()
    try:
        result = await cls(node).run(context)
    except NodeExecutionError as exc:
        return {
            "ok": False,
            "status": "error",
            "node_id": node.id,
            "node_type": node.type,
            "output": None,
            "outputs": None,
            "error": str(exc),
            "duration_ms": int((time.monotonic() - started) * 1000),
            "pinned_used": False,
        }
    outputs = result.outputs or {}
    return {
        "ok": True,
        "status": "success",
        "node_id": node.id,
        "node_type": node.type,
        "output": outputs.get("main") if "main" in outputs else outputs,
        "outputs": outputs,
        "error": None,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "pinned_used": False,
    }


@router.get("/{workflow_id}/webhook-url", response_model=dict)
async def webhook_url(workflow_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    wf = await db.get(Workflow, workflow_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    from ..services.webhook_info import public_webhook_url

    nodes = wf.webhook_nodes()
    return {
        "workflow_id": workflow_id,
        "has_webhook_node": bool(nodes),
        "url": public_webhook_url(request, workflow_id),
        "response_mode": (nodes[0].get("parameters") or {}).get("response_mode", "immediately") if nodes else None,
    }


# ----------------------------------------------------------------------
# Export / import / duplicate (portability + sharing between instances)
# ----------------------------------------------------------------------
@router.get("/{workflow_id}/export", response_model=WorkflowExportDoc)
async def export_workflow(workflow_id: str, db: AsyncSession = Depends(get_db)):
    wf = await db.get(Workflow, workflow_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return WorkflowExportDoc(
        name=wf.name,
        description=wf.description or "",
        graph=wf.graph or {"nodes": [], "edges": []},
    )


@router.post("/import", response_model=WorkflowOut, status_code=201)
async def import_workflow(body: WorkflowImportRequest, db: AsyncSession = Depends(get_db)):
    doc = body.data
    if doc is not None:
        name, description, graph = doc.name, doc.description, doc.graph
    elif body.graph is not None and body.name:
        name, description, graph = body.name, body.description or "", body.graph
    else:
        raise HTTPException(status_code=400, detail="Provide {data: {...}} or bare {name, graph}")

    if not name or not str(name).strip():
        raise HTTPException(status_code=400, detail="Imported workflow has no name")
    try:
        graph = validate_graph_document(graph or {"nodes": [], "edges": []}).model_dump()
    except (GraphValidationError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid graph: {exc}") from exc

    # Fresh identity: strip stale node ids collisions is unnecessary (ids are
    # graph-local), but always start inactive so imports never auto-fire.
    wf = Workflow(name=str(name).strip(), description=description, graph=graph, is_active=False)
    db.add(wf)
    await db.flush()
    await db.refresh(wf)
    await snapshot_workflow_version(db, wf)  # v1 — the imported state
    await db.commit()  # avoid teardown-commit race
    await resync_workflow_jobs(wf.id)
    return wf


@router.post("/{workflow_id}/duplicate", response_model=WorkflowOut, status_code=201)
async def duplicate_workflow(workflow_id: str, db: AsyncSession = Depends(get_db)):
    wf = await db.get(Workflow, workflow_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    copy = Workflow(
        name=f"{wf.name} (copy)"[:200],
        description=wf.description or "",
        graph=wf.graph or {"nodes": [], "edges": []},
        is_active=False,
        tags=list(wf.tags or []),
        folder_id=wf.folder_id,  # duplicates stay organized next to the original
    )
    db.add(copy)
    await db.flush()
    await db.refresh(copy)
    await snapshot_workflow_version(db, copy)  # v1 — the duplicated state
    await db.commit()  # avoid teardown-commit race
    await resync_workflow_jobs(copy.id)
    return copy


# ----------------------------------------------------------------------
# Version history (v13) — bounded snapshot list + restore
# ----------------------------------------------------------------------
@router.get("/{workflow_id}/versions")
async def list_versions(workflow_id: str, db: AsyncSession = Depends(get_db)):
    wf = await db.get(Workflow, workflow_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    rows = (
        await db.execute(
            select(
                WorkflowVersion.version,
                WorkflowVersion.name,
                WorkflowVersion.description,
                WorkflowVersion.node_count,
                WorkflowVersion.created_at,
            )
            .where(WorkflowVersion.workflow_id == workflow_id)
            .order_by(WorkflowVersion.version.desc())
        )
    ).all()
    latest = rows[0][0] if rows else None
    return {
        "workflow_id": workflow_id,
        "max_versions": MAX_VERSIONS,
        "latest": latest,
        "versions": [
            {
                "version": r[0],
                "name": r[1],
                "description": r[2],
                "node_count": r[3],
                "created_at": r[4].isoformat() if r[4] else None,
                "is_current": latest is not None and r[0] == latest,
            }
            for r in rows
        ],
    }


@router.get("/{workflow_id}/versions/{version}")
async def get_version(workflow_id: str, version: int, db: AsyncSession = Depends(get_db)):
    snap = (
        await db.execute(
            select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == workflow_id,
                WorkflowVersion.version == version,
            )
        )
    ).scalar_one_or_none()
    if snap is None:
        raise HTTPException(status_code=404, detail="Version not found")
    return {
        "workflow_id": workflow_id,
        "version": snap.version,
        "name": snap.name,
        "description": snap.description,
        "graph": snap.graph,
        "tags": snap.tags or [],
        "node_count": snap.node_count,
        "created_at": snap.created_at.isoformat() if snap.created_at else None,
    }


@router.post("/{workflow_id}/versions/{version}/restore", response_model=WorkflowOut)
async def restore_version(workflow_id: str, version: int, db: AsyncSession = Depends(get_db)):
    """Roll the workflow back to a snapshot's content (name/description/graph).

    The restore itself lands as a NEW version on top of the history — nothing
    is destroyed, so redo is just "restore the version that was current".
    Tags / activation / error binding are left untouched.
    """
    wf = await db.get(Workflow, workflow_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    snap = (
        await db.execute(
            select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == workflow_id,
                WorkflowVersion.version == version,
            )
        )
    ).scalar_one_or_none()
    if snap is None:
        raise HTTPException(status_code=404, detail="Version not found")

    wf.name = snap.name
    wf.description = snap.description or ""
    wf.graph = snap.graph or {"nodes": [], "edges": []}
    await db.flush()
    await db.refresh(wf)
    await snapshot_workflow_version(db, wf)  # restore = new version on top
    await db.commit()  # avoid teardown-commit race
    await resync_workflow_jobs(workflow_id)  # snapshot may change schedule nodes
    return wf
