"""Workflow templates — curated one-click starting points."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..engine.runner import GraphValidationError, validate_graph_document
from ..models import Workflow
from ..schemas import WorkflowOut
from ..services.scheduler import resync_workflow_jobs
from ..services.templates import TEMPLATES, get_template, template_summary
from ..services.versions import snapshot_workflow_version

router = APIRouter(prefix="/templates", tags=["templates"])


@router.get("")
async def list_templates():
    return [template_summary(t) for t in TEMPLATES]


@router.get("/{template_id}")
async def get_template_detail(template_id: str):
    t = get_template(template_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return {**template_summary(t), "graph": t["graph"]}


@router.post("/{template_id}/use", response_model=WorkflowOut, status_code=201)
async def use_template(template_id: str, db: AsyncSession = Depends(get_db)):
    """Instantiate a template as a real (inactive) workflow."""
    t = get_template(template_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Template not found")
    try:
        graph = validate_graph_document(t["graph"]).model_dump()
    except (GraphValidationError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"Template graph invalid: {exc}") from exc

    wf = Workflow(
        name=t["name"],
        description=t["description"],
        graph=graph,
        is_active=False,
    )
    db.add(wf)
    await db.flush()
    await db.refresh(wf)
    await snapshot_workflow_version(db, wf)  # v1 — the instantiated state
    await db.commit()  # commit before responding (teardown-commit race guard)
    await resync_workflow_jobs(wf.id)
    return wf
