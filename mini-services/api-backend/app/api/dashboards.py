"""Dashboards API (v31) — read-only analytics boards over MANY datasets.

Admin endpoints (resolve by id OR case-insensitive name)
----------------------------------------------------------
GET    /dashboards                    list
POST   /dashboards                    create (generate from dataset_ids, or explicit config, or blank)
GET    /dashboards/{ref}              metadata
PATCH  /dashboards/{ref}              rename (re-slugs) / re-describe / set config
DELETE /dashboards/{ref}              drop board (datasets untouched)
POST   /dashboards/{ref}/generate     re-generate config from fresh dataset order
POST   /dashboards/{ref}/preview      compute the CURRENT config (drafts welcome) — builder live preview
POST   /dashboards/{ref}/publish      draft → published (guards: ≥1 component, all datasets resolve, config valid)
POST   /dashboards/{ref}/unpublish    published → draft

Runtime endpoint (slug-addressable, PUBLISHED boards only)
----------------------------------------------------------
GET    /dashboards/{slug}/runtime     board + rendered component payload for /d/{slug}

Every mutation commits explicitly (v4 lesson). Datasets referenced by a
component are validated against the LIVE dataset table on write; compute
degrades to empty content if a dataset vanishes later (boards stay
renderable, never 500).
"""

from __future__ import annotations

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import Dashboard, Dataset
from ..schemas import DashboardCreate, DashboardOut, DashboardUpdate
from ..services import dashboards as db_svc
from ..services import datasets as ds_svc

router = APIRouter(prefix="/dashboards", tags=["dashboards"])


def _out(row: Dashboard) -> DashboardOut:
    return DashboardOut(
        id=row.id,
        name=row.name,
        slug=row.slug,
        description=row.description or "",
        config=row.config or {},
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _get_or_404(db: AsyncSession, ref: str) -> Dashboard:
    row = await db_svc.get_dashboard(db, ref)
    if row is None:
        raise HTTPException(status_code=404, detail="Dashboard not found")
    return row


async def _runtime_or_404(db: AsyncSession, slug: str) -> Dashboard:
    row = await db_svc.get_by_slug(db, slug)
    if row is None or row.status != "published":
        raise HTTPException(status_code=404, detail="Dashboard not found (or not published)")
    return row


def _refs(components: list[dict]) -> list[str]:
    """Deduped dataset_id references from a component list, order kept."""
    return [
        c.get("dataset_id")
        for c in components
        if isinstance(c, dict) and c.get("dataset_id")
    ]


async def _collect_datasets(
    db: AsyncSession, dataset_ids: list[str], strict: bool = True
) -> dict[str, Dataset]:
    """Resolve referenced dataset ids (deduped).

    strict=True (writes/publish): 404 on the first missing id.
    strict=False (compute): missing datasets are SKIPPED so boards stay
    renderable when a component outlives its dataset.
    """
    out: dict[str, Dataset] = {}
    for ds_id in dict.fromkeys(dataset_ids):
        row = await db.get(Dataset, ds_id)
        if row is None:
            if strict:
                raise HTTPException(status_code=404, detail=f"Dataset {ds_id!r} not found")
            continue
        out[ds_id] = row
    return out


async def _load_frames(datasets: dict[str, Dataset]) -> dict[str, pd.DataFrame]:
    return {ds_id: db_svc._load_df(ds) for ds_id, ds in datasets.items()}


async def _validate_or_400(config: dict, datasets: dict[str, Dataset]) -> None:
    try:
        db_svc.validate_config(config, {k: v.schema_json or [] for k, v in datasets.items()})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _load_generators(db: AsyncSession, dataset_ids: list[str]):
    """dataset_ids → ordered [(Dataset, DataFrame)] for generate_config."""
    rows = await _collect_datasets(db, dataset_ids)
    frames = await _load_frames(rows)
    return [(rows[ds_id], frames[ds_id]) for ds_id in rows]


async def _compute_payload(row: Dashboard, db: AsyncSession) -> dict:
    """Rendered board payload — shared by preview and runtime (tolerant)."""
    components = (row.config or {}).get("components", [])
    datasets = await _collect_datasets(db, _refs(components), strict=False)
    frames = await _load_frames(datasets)
    return db_svc.compute_config(components, frames)


# ----------------------------------------------------------------- admin
@router.get("", response_model=list[DashboardOut])
async def list_dashboards(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Dashboard).order_by(Dashboard.updated_at.desc()))).scalars().all()
    return [_out(r) for r in rows]


@router.post("", response_model=DashboardOut, status_code=201)
async def create_dashboard(body: DashboardCreate, db: AsyncSession = Depends(get_db)):
    name = body.name.strip()
    if not ds_svc.NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail="Name must start with a letter or digit and contain only letters, digits, spaces, dots, dashes or underscores",
        )
    if await db_svc.name_taken(db, name):
        raise HTTPException(status_code=409, detail=f"Dashboard {name!r} already exists")

    if body.config is not None:
        config = body.config
        datasets = await _collect_datasets(db, _refs(config.get("components", [])))
        await _validate_or_400(config, datasets)
    elif body.generate and body.dataset_ids:
        pairs = await _load_generators(db, body.dataset_ids)
        config = db_svc.generate_config(pairs)
    else:
        # No config and nothing to generate from (generate defaults to True,
        # so blank creates land here too — same tolerance as apps).
        config = {"components": []}

    row = Dashboard(
        name=name,
        slug=await db_svc.unique_slug(db, name),
        description=body.description.strip(),
        config=config,
        status="draft",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _out(row)


@router.get("/{dash_ref}", response_model=DashboardOut)
async def get_dashboard(dash_ref: str, db: AsyncSession = Depends(get_db)):
    return _out(await _get_or_404(db, dash_ref))


@router.patch("/{dash_ref}", response_model=DashboardOut)
async def update_dashboard(dash_ref: str, body: DashboardUpdate, db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, dash_ref)
    if body.name is not None:
        name = body.name.strip()
        if not ds_svc.NAME_RE.match(name):
            raise HTTPException(status_code=400, detail="Invalid dashboard name")
        if await db_svc.name_taken(db, name, exclude_id=row.id):
            raise HTTPException(status_code=409, detail=f"Dashboard {name!r} already exists")
        row.name = name
        row.slug = await db_svc.unique_slug(db, name, exclude_id=row.id)
    if body.description is not None:
        row.description = body.description.strip()
    if body.config is not None:
        if row.status == "published":
            raise HTTPException(status_code=409, detail="Unpublish before editing the config")
        datasets = await _collect_datasets(db, _refs(body.config.get("components", [])))
        await _validate_or_400(body.config, datasets)
        row.config = body.config
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _out(row)


@router.delete("/{dash_ref}", status_code=204)
async def delete_dashboard(dash_ref: str, db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, dash_ref)
    await db.delete(row)
    await db.commit()


@router.post("/{dash_ref}/generate", response_model=DashboardOut)
async def regenerate_dashboard(dash_ref: str, db: AsyncSession = Depends(get_db)):
    """Re-generate the layout from the datasets the current components reference."""
    row = await _get_or_404(db, dash_ref)
    if row.status == "published":
        raise HTTPException(status_code=409, detail="Unpublish before regenerating")
    refs = _refs((row.config or {}).get("components", []))
    if not refs:
        raise HTTPException(status_code=409, detail="No datasets referenced yet — add a component first")
    pairs = await _load_generators(db, refs)
    row.config = db_svc.generate_config(pairs)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _out(row)


@router.post("/{dash_ref}/preview")
async def preview_dashboard(dash_ref: str, db: AsyncSession = Depends(get_db)):
    """Compute the CURRENT config — the builder's live data preview (drafts OK)."""
    row = await _get_or_404(db, dash_ref)
    return {
        "dashboard": {"name": row.name, "slug": row.slug, "status": row.status},
        "components": await _compute_payload(row, db),
    }


@router.post("/{dash_ref}/publish", response_model=DashboardOut)
async def publish_dashboard(dash_ref: str, db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, dash_ref)
    components = (row.config or {}).get("components", [])
    datasets = await _collect_datasets(db, _refs(components))
    await _validate_or_400(row.config or {}, datasets)
    row.status = "published"
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _out(row)


@router.post("/{dash_ref}/unpublish", response_model=DashboardOut)
async def unpublish_dashboard(dash_ref: str, db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, dash_ref)
    row.status = "draft"
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _out(row)


# ----------------------------------------------------------------- runtime
@router.get("/{slug}/runtime")
async def runtime(slug: str, db: AsyncSession = Depends(get_db)):
    row = await _runtime_or_404(db, slug)
    components = (row.config or {}).get("components", [])
    datasets = await _collect_datasets(db, _refs(components), strict=False)
    dataset_meta = [
        {"id": ds.id, "name": ds.name, "row_count": ds.row_count} for ds in datasets.values()
    ]
    frames = await _load_frames(datasets)
    return {
        "dashboard": {
            "name": row.name,
            "slug": row.slug,
            "description": row.description or "",
            "status": row.status,
        },
        "datasets": dataset_meta,
        "components": db_svc.compute_config(components, frames),
    }
