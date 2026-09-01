"""Template packs (v39) - multi-resource portability for Py8n.

A pack is a single JSON document bundling several workflows and dataset
snapshots so an estate can move between instances, be shared with a
teammate or archived as a backup. Format marker: ``format == "py8n-pack"``.

Endpoints:
  POST /packs/export   build a pack from explicit resource ids
  POST /packs/inspect  preview what an import would do (no writes)
  POST /packs/import   create the resources, return a per-item summary
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user, own_or_404
from ..config import settings
from ..db import get_db
from ..engine.runner import GraphValidationError, validate_graph_document
from ..models import Dataset, Workflow
from ..services import datasets as ds_svc
from ..services.scheduler import resync_workflow_jobs
from ..services.versions import snapshot_workflow_version

router = APIRouter(prefix="/packs", tags=["packs"])

PACK_FORMAT = "py8n-pack"
PACK_VERSION = 1
# Keep packs sane: a dataset snapshot larger than this is truncated with a
# warning instead of producing a multi-GB JSON blob.
MAX_PACK_ROWS = 100_000


# ------------------------------------------------------------------ models
class PackExportRequest(BaseModel):
    workflow_ids: list[str] = Field(default_factory=list)
    dataset_ids: list[str] = Field(default_factory=list)
    include_rows: bool = True  # false = schema-only dataset snapshots


class PackWorkflow(BaseModel):
    name: str
    description: str = ""
    graph: dict


class PackDataset(BaseModel):
    model_config = ConfigDict(populate_by_name=True)  # accept "schema" on the wire

    name: str
    description: str = ""
    # wire key is "schema"; the python name avoids shadowing BaseModel.schema_json
    schema_def: list = Field(default_factory=list, alias="schema")
    rows: list = Field(default_factory=list)


class PackDocument(BaseModel):
    """Inbound pack (import / inspect). Unknown extra keys are ignored."""

    format: str = PACK_FORMAT
    pack_version: int = PACK_VERSION
    generated_at: str | None = None
    py8n_version: str | None = None
    workflows: list[PackWorkflow] = Field(default_factory=list)
    datasets: list[PackDataset] = Field(default_factory=list)


# ------------------------------------------------------------------ helpers
def _node_types(graph: dict) -> list[str]:
    types: list[str] = []
    for node in (graph or {}).get("nodes", []):
        t = (node or {}).get("type")
        if t and t not in types:
            types.append(t)
    return types


async def _unique_dataset_name(db: AsyncSession, base: str) -> str:
    """Datasets are unique by name - collide with a numbered suffix."""
    name = base
    for i in range(2, 60):
        if not await ds_svc.name_taken(db, name):
            return name
        name = f"{base} ({i})"
    return f"{base} (pack-{ds_svc.parquet_path('x').stem})"[:118]


# ------------------------------------------------------------------ export
@router.post("/export")
async def export_pack(body: PackExportRequest, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    if not body.workflow_ids and not body.dataset_ids:
        raise HTTPException(status_code=400, detail="Pick at least one workflow or dataset to pack")

    workflows: list[dict] = []
    warnings: list[str] = []
    node_types: list[str] = []
    for wid in dict.fromkeys(body.workflow_ids):  # de-dup, keep order
        wf = await db.get(Workflow, wid)
        if wf is None:
            warnings.append(f"workflow {wid} not found, skipped")
            continue
        own_or_404(wf.owner_id, user)  # v37: foreign rows look nonexistent
        graph = wf.graph or {"nodes": [], "edges": []}
        workflows.append({"name": wf.name, "description": wf.description or "", "graph": graph})
        for t in _node_types(graph):
            if t not in node_types:
                node_types.append(t)

    datasets: list[dict] = []
    total_rows = 0
    for did in dict.fromkeys(body.dataset_ids):
        ds = await db.get(Dataset, did)
        if ds is None:
            warnings.append(f"dataset {did} not found, skipped")
            continue
        own_or_404(ds.owner_id, user)  # v37
        rows: list = []
        if body.include_rows and ds.row_count:
            df = ds_svc.read_parquet_df(ds_svc.parquet_path(ds.id))
            rows = ds_svc.jsonable_rows(df)
            if len(rows) > MAX_PACK_ROWS:
                rows = rows[:MAX_PACK_ROWS]
                warnings.append(f"dataset '{ds.name}' truncated to {MAX_PACK_ROWS} rows")
        total_rows += len(rows)
        datasets.append(
            {
                "name": ds.name,
                "description": ds.description or "",
                "schema": ds.schema_json or [],
                "rows": rows,
            }
        )
    if not workflows and not datasets:
        raise HTTPException(status_code=404, detail="None of the requested resources exist")

    return {
        "format": PACK_FORMAT,
        "pack_version": PACK_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "py8n_version": settings.version,
        "manifest": {
            "workflow_count": len(workflows),
            "dataset_count": len(datasets),
            "node_types": node_types,
            "total_rows": total_rows,
            "include_rows": body.include_rows,
            "warnings": warnings,
        },
        "workflows": workflows,
        "datasets": datasets,
    }


# ------------------------------------------------------------------ inspect
async def _inspect_pack_doc(pack: PackDocument, db: AsyncSession) -> dict:
    """Dry-run a pack against this instance without touching the database.
    Shared by POST /packs/inspect and registry checks (v43)."""
    warnings: list[str] = []
    wf_previews = []
    for w in pack.workflows:
        name = (w.name or "").strip() or "Untitled workflow"
        error: str | None = None
        try:
            validate_graph_document(w.graph or {"nodes": [], "edges": []})
        except (GraphValidationError, ValueError) as exc:
            error = str(exc)
        existing = (
            await db.execute(select(Workflow.id).where(Workflow.name == name).limit(1))
        ).scalar_one_or_none()
        wf_previews.append(
            {
                "name": name,
                "node_count": len((w.graph or {}).get("nodes", [])),
                "valid": error is None,
                "error": error,
                "exists": existing is not None,
            }
        )

    ds_previews = []
    for d in pack.datasets:
        name = (d.name or "").strip()
        if not name or not ds_svc.NAME_RE.match(name):
            ds_previews.append({"name": name, "rows": len(d.rows), "rename_to": None, "invalid_name": True})
            warnings.append(f"dataset name {name!r} is invalid on this instance, it will be skipped")
            continue
        target = await _unique_dataset_name(db, name)
        ds_previews.append({"name": name, "rows": len(d.rows), "rename_to": None if target == name else target, "invalid_name": False})

    return {
        "format": pack.format,
        "py8n_version": pack.py8n_version,
        "workflow_count": len(wf_previews),
        "dataset_count": len(ds_previews),
        "workflows": wf_previews,
        "datasets": ds_previews,
        "warnings": warnings,
    }


@router.post("/inspect")
async def inspect_pack(pack: PackDocument, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Preview an import without touching the database (dialog support)."""
    if pack.format != PACK_FORMAT:
        raise HTTPException(status_code=400, detail=f"Not a Py8n pack (format {pack.format!r})")
    return await _inspect_pack_doc(pack, db)


# ------------------------------------------------------------------ import
async def _import_pack_doc(pack: PackDocument, owner: str | None, db: AsyncSession) -> dict:
    """Create every valid resource in the pack (shared by POST /packs/import
    and registry syncs, v43). Invalid entries are skipped with reasons - one
    bad graph never aborts the batch."""
    created_workflows = []
    skipped = []
    warnings: list[str] = []

    for w in pack.workflows:
        name = (w.name or "").strip() or "Untitled workflow"
        try:
            graph = validate_graph_document(w.graph or {"nodes": [], "edges": []}).model_dump()
        except (GraphValidationError, ValueError) as exc:
            skipped.append({"name": name, "reason": str(exc)})
            continue
        wf = Workflow(name=name[:200], description=w.description or "", graph=graph, is_active=False)
        wf.owner_id = owner
        db.add(wf)
        await db.flush()
        await db.refresh(wf)
        await snapshot_workflow_version(db, wf)  # v1 - imported state
        created_workflows.append(wf)

    created_datasets = []
    for d in pack.datasets:
        name = (d.name or "").strip()
        if not name or not ds_svc.NAME_RE.match(name):
            skipped.append({"name": name, "reason": "invalid dataset name"})
            continue
        final_name = await _unique_dataset_name(db, name)
        df = ds_svc.normalize_df(pd.DataFrame(d.rows)) if d.rows else pd.DataFrame()
        ds = await ds_svc.create_from_df(db, final_name, df, source="import", description=d.description or "", owner_id=owner)
        created_datasets.append(ds)

    await db.commit()
    for wf in created_workflows:
        await db.refresh(wf)
    for ds in created_datasets:
        await db.refresh(ds)
    for wf in created_workflows:
        await resync_workflow_jobs(wf.id)

    return {
        "workflows": [{"id": wf.id, "name": wf.name, "node_count": len((wf.graph or {}).get("nodes", []))} for wf in created_workflows],
        "datasets": [{"id": ds.id, "name": ds.name, "row_count": ds.row_count} for ds in created_datasets],
        "skipped": skipped,
        "warnings": warnings,
    }


@router.post("/import", status_code=201)
async def import_pack(pack: PackDocument, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    if pack.format != PACK_FORMAT:
        raise HTTPException(status_code=400, detail=f"Not a Py8n pack (format {pack.format!r})")
    if not pack.workflows and not pack.datasets:
        raise HTTPException(status_code=400, detail="Pack contains no workflows or datasets")

    owner = user.id if user else None  # v37
    return await _import_pack_doc(pack, owner, db)
