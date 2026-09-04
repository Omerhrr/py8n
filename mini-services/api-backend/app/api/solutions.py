"""Solution Marketplace API (v60) - gallery + packs, sold as outcomes.

* ``GET    /solutions``                 - the marketplace shelf (q/category filters)
* ``GET    /solutions/{slug}``          - capability checklist + embedded pack summary
* ``POST   /solutions/{slug}/install``  - import the pack into YOUR estate
* ``POST   /solutions``                 - author a solution from your own content
* ``DELETE /solutions/{slug}``          - unlist an authored solution (curator only)

Installing reuses the exact pack-import machinery (``_import_pack_doc``):
every workflow lands inactive, datasets carry their sample rows, and the
response returns the created refs. The shelf self-seeds the three curated
showcase solutions on first read (idempotent by slug).
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user, own_or_404
from ..db import get_db
from ..models import Dataset, Solution, Workflow
from ..api.packs import PackDocument, _import_pack_doc
from ..services.solutions import (
    MODEL_SYSTEM_MODALITIES,
    ensure_seeded,
    finalize_pack_dataset_names,
    finalize_pack_model_names,
    pack_summary,
    solution_summary,
)

router = APIRouter(prefix="/solutions", tags=["solutions"])

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,120}$")


class SolutionInstallRequest(BaseModel):
    note: str = Field(default="", max_length=300, description="Optional install note for the response")
    as_system: bool = Field(default=False, description="v61: also create a Py8n System binding everything this install created")
    as_model_system: bool = Field(default=False, description="v64: also create a Model System (datasets + training/serving workflows as one operating unit)")


class SolutionAuthorRequest(BaseModel):
    name: str = Field(..., min_length=3, max_length=140)
    tagline: str = Field(default="", max_length=300)
    category: str = Field(default="Operations", max_length=60)
    icon: str = Field(default="package", max_length=60)
    color: str = Field(default="#22d3ee", max_length=20)
    outcomes: list[str] = Field(..., min_length=1, max_length=12,
                                description="The capability checklist - what the user GETS")
    docs: str = Field(default="", max_length=4000)
    workflow_ids: list[str] = Field(default_factory=list, max_length=20)
    dataset_ids: list[str] = Field(default_factory=list, max_length=20)
    include_rows: bool = Field(default=True, description="Bundle sample rows for datasets")


async def _get_solution(db: AsyncSession, slug: str) -> Solution:
    row = (await db.execute(select(Solution).where(Solution.slug == slug))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Solution not found")
    return row


@router.get("")
async def list_solutions(
    q: str = "",
    category: str = "",
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    await ensure_seeded(db)
    await db.commit()
    rows = (await db.execute(select(Solution).order_by(Solution.installs.desc(), Solution.created_at.desc()))).scalars().all()
    needle = (q or "").strip().lower()
    out = []
    for s in rows:
        if category and s.category.lower() != category.strip().lower():
            continue
        if needle and needle not in f"{s.name} {s.tagline} {s.category} {' '.join(s.outcomes_json or [])}".lower():
            continue
        out.append(solution_summary(s))
    categories = sorted({s.category for s in rows})
    return {"solutions": out, "categories": categories, "total": len(out)}


@router.get("/{slug}")
async def solution_detail(slug: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    await ensure_seeded(db)
    await db.commit()
    s = await _get_solution(db, slug)
    return {**solution_summary(s), "docs": s.docs, "pack": pack_summary(s)}


@router.post("/{slug}/install")
async def install_solution(slug: str, body: SolutionInstallRequest | None = None,
                           user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Import the solution's pack into your estate (workflows inactive)."""
    await ensure_seeded(db)
    await db.commit()
    s = await _get_solution(db, slug)
    owner = user.id if user else None
    pack_dict = await finalize_pack_dataset_names(db, s.pack_json or {})
    pack_dict = await finalize_pack_model_names(db, pack_dict, owner)
    pack = PackDocument.model_validate(pack_dict)
    result = await _import_pack_doc(pack, owner, db)
    s.installs = int(s.installs or 0) + 1

    system_ref = None
    if body and body.as_system:
        from ..models import Py8nSystem, SystemComponent

        sys_row = Py8nSystem(
            name=f"{s.name} system",
            description=f"Installed from the '{s.name}' solution - " + (body.note or s.tagline or "")[:400],
            icon=s.icon, color=s.color,
        )
        sys_row.owner_id = owner
        db.add(sys_row)
        await db.flush()
        for wf in result.get("workflows", []):
            db.add(SystemComponent(system_id=sys_row.id, kind="workflow", ref_id=wf["id"]))
        for ds in result.get("datasets", []):
            db.add(SystemComponent(system_id=sys_row.id, kind="dataset", ref_id=ds["id"]))
        await db.flush()
        system_ref = {"id": sys_row.id, "name": sys_row.name}

    model_system_ref = None
    if body and body.as_model_system:
        from ..models import ModelSystem, ModelSystemComponent
        from ..services.model_systems import MODALITY_NODE_TYPES

        # declared modalities for curated model solutions, otherwise derived
        # from the pack's own node-type evidence (fail-honest fallback: text
        # is NOT assumed - a pack with no modality nodes declares none)
        declared = list(MODEL_SYSTEM_MODALITIES.get(s.slug, []))
        if not declared:
            evidence: set[str] = set()
            for w in (s.pack_json or {}).get("workflows", []):
                for n in (w.get("graph") or {}).get("nodes", []):
                    mod = MODALITY_NODE_TYPES.get(n.get("type") or "")
                    if mod:
                        evidence.add(mod)
            declared = sorted(evidence)
        ms_row = ModelSystem(
            name=f"{s.name} model system",
            description=f"Installed from the '{s.name}' solution - " + (body.note or s.tagline or "")[:400],
            icon=s.icon if s.icon != "package" else "brain-circuit",
            color=s.color,
            modalities=declared,
        )
        ms_row.owner_id = owner
        db.add(ms_row)
        await db.flush()
        for wf in result.get("workflows", []):
            db.add(ModelSystemComponent(model_system_id=ms_row.id, kind="workflow", ref_id=wf["id"]))
        for ds in result.get("datasets", []):
            db.add(ModelSystemComponent(model_system_id=ms_row.id, kind="dataset", ref_id=ds["id"]))
        await db.flush()
        model_system_ref = {"id": ms_row.id, "name": ms_row.name, "modalities": declared}

    await db.commit()
    return {
        "slug": s.slug,
        "name": s.name,
        "installs": s.installs,
        "note": body.note if body else "",
        "created_workflows": result.get("workflows", []),
        "created_datasets": result.get("datasets", []),
        "skipped": result.get("skipped", []),
        "warnings": result.get("warnings", []),
        "system": system_ref,
        "model_system": model_system_ref,
    }


@router.post("", status_code=201)
async def author_solution(body: SolutionAuthorRequest, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Publish your own workflows/datasets as an outcome-named solution."""
    if not body.workflow_ids and not body.dataset_ids:
        raise HTTPException(status_code=400, detail="Pick at least one workflow or dataset to publish")

    workflows: list[dict] = []
    for wid in dict.fromkeys(body.workflow_ids):
        wf = await db.get(Workflow, wid)
        if wf is None:
            raise HTTPException(status_code=404, detail=f"Workflow {wid} not found")
        own_or_404(wf.owner_id, user)
        workflows.append({"name": wf.name, "description": wf.description or "",
                          "graph": wf.graph or {"nodes": [], "edges": []}})

    datasets: list[dict] = []
    for did in dict.fromkeys(body.dataset_ids):
        ds = await db.get(Dataset, did)
        if ds is None:
            raise HTTPException(status_code=404, detail=f"Dataset {did} not found")
        own_or_404(ds.owner_id, user)
        rows: list = []
        if body.include_rows and ds.row_count:
            from ..services import datasets as ds_svc

            df = ds_svc.read_parquet_df(ds_svc.parquet_path(ds.id))
            rows = ds_svc.jsonable_rows(df)[:500]
        datasets.append({"name": ds.name, "description": ds.description or "",
                         "schema": ds.schema_json or [], "rows": rows})

    slug = re.sub(r"[^a-z0-9]+", "-", body.name.strip().lower()).strip("-")[:120] or "solution"
    if not SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="Could not derive a valid slug from the name")
    if (await db.execute(select(Solution).where(Solution.slug == slug))).scalar_one_or_none():
        import uuid as _uuid

        slug = f"{slug}-{_uuid.uuid4().hex[:6]}"

    outcomes = [o.strip()[:200] for o in body.outcomes if o and o.strip()]
    row = Solution(
        slug=slug, name=body.name.strip(), tagline=body.tagline.strip(),
        category=body.category.strip() or "Operations", icon=body.icon, color=body.color,
        outcomes_json=outcomes,
        pack_json={"format": "py8n-pack", "pack_version": 1,
                   "workflows": workflows, "datasets": datasets},
        docs=body.docs, installs=0,
    )
    row.owner_id = user.id if user else None
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return solution_summary(row)


@router.delete("/{slug}", status_code=204)
async def delete_solution(slug: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    s = await _get_solution(db, slug)
    own_or_404(s.owner_id, user)  # NULL-owner curated rows stay 404 for everyone
    await db.delete(s)
    await db.commit()
