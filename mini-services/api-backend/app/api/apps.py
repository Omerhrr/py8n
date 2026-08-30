"""Apps API (v29) — the Excel → App builder flagship.

Admin endpoints (resolve by id OR case-insensitive name)
--------------------------------------------------------
GET    /apps                        list
POST   /apps                        create (blank, or generate from dataset)
GET    /apps/{ref}                  metadata
PATCH  /apps/{ref}                  rename (re-slugs) / re-describe / bind dataset / set config
DELETE /apps/{ref}                  drop app (dataset untouched)
POST   /apps/{ref}/generate         re-generate config from the bound dataset
POST   /apps/{ref}/publish          draft → published (guards: dataset + valid config)
POST   /apps/{ref}/unpublish        published → draft

Runtime endpoints (slug-addressable, PUBLISHED apps only)
---------------------------------------------------------
GET    /apps/{slug}/runtime              app + dataset schema + stats + chart data
GET    /apps/{slug}/records              paginated rows
POST   /apps/{slug}/records              create a record (lands in the dataset parquet)
PATCH  /apps/{slug}/records/{index}      edit a record
DELETE /apps/{slug}/records/{index}      delete a record
GET    /apps/{slug}/form                 standalone form descriptor (v30)
POST   /apps/{slug}/form-submit          anonymous form submission (v30)

Rules management (v30) — the config lock does NOT apply: rules are
governance, editable on live apps without touching the layout
------------------------------------------------------------------------------
GET    /apps/{ref}/rules                 rules + the known ops/actions/events
PUT    /apps/{ref}/rules                 replace all rules (validated)
POST   /apps/{ref}/rules/test            dry-run a sample record against the rules

Every mutation commits explicitly (v4 lesson).
"""

from __future__ import annotations

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import App, Dataset
from ..schemas import AppCreate, AppOut, AppRecordIn, AppUpdate, RulesTestIn, RulesPut
from ..services import apps as app_svc
from ..services import datasets as ds_svc
from ..services import rules as rule_svc

router = APIRouter(prefix="/apps", tags=["apps"])


def _out(row: App, dataset: Dataset | None = None) -> AppOut:
    return AppOut(
        id=row.id,
        name=row.name,
        slug=row.slug,
        description=row.description or "",
        dataset_id=row.dataset_id,
        dataset_name=dataset.name if dataset else None,
        config=row.config or {},
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _dataset_for(db: AsyncSession, app_row: App) -> Dataset | None:
    if not app_row.dataset_id:
        return None
    return await db.get(Dataset, app_row.dataset_id)


async def _out_with_dataset(db: AsyncSession, row: App) -> AppOut:
    return _out(row, await _dataset_for(db, row))


async def _get_or_404(db: AsyncSession, ref: str) -> App:
    row = await app_svc.get_app(db, ref)
    if row is None:
        raise HTTPException(status_code=404, detail="App not found")
    return row


async def _runtime_or_404(db: AsyncSession, slug: str) -> App:
    row = await app_svc.get_by_slug(db, slug)
    if row is None or row.status != "published":
        raise HTTPException(status_code=404, detail="App not found (or not published)")
    return row


def _form_comp(row: App) -> dict | None:
    """First form component, if any (v30 forms + rules key off it)."""
    for comp in (row.config or {}).get("components", []):
        if comp.get("type") == "form":
            return comp
    return None


# ----------------------------------------------------------------- admin
@router.get("", response_model=list[AppOut])
async def list_apps(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(App).order_by(App.updated_at.desc()))).scalars().all()
    return [await _out_with_dataset(db, r) for r in rows]


@router.post("", response_model=AppOut, status_code=201)
async def create_app(body: AppCreate, db: AsyncSession = Depends(get_db)):
    name = body.name.strip()
    if not ds_svc.NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail="Name must start with a letter or digit and contain only letters, digits, spaces, dots, dashes or underscores",
        )
    if await app_svc.name_taken(db, name):
        raise HTTPException(status_code=409, detail=f"App {name!r} already exists")

    dataset = None
    if body.dataset_id:
        dataset = await db.get(Dataset, body.dataset_id)
        if dataset is None:
            raise HTTPException(status_code=404, detail="Dataset not found")

    config = body.config
    if config is None and dataset is not None and body.generate:
        df = ds_svc.read_parquet_df(ds_svc.parquet_path(dataset.id))
        config = app_svc.generate_config(df, dataset.schema_json or [])
    config = config or {"components": []}
    if dataset is not None:
        try:
            app_svc.validate_config(config, dataset.schema_json or [])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    row = App(
        name=name,
        slug=await app_svc.unique_slug(db, name),
        description=body.description.strip(),
        dataset_id=dataset.id if dataset else None,
        config=config,
        status="draft",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _out(row, dataset)


@router.get("/{app_ref}", response_model=AppOut)
async def get_app(app_ref: str, db: AsyncSession = Depends(get_db)):
    return await _out_with_dataset(db, await _get_or_404(db, app_ref))


@router.patch("/{app_ref}", response_model=AppOut)
async def update_app(app_ref: str, body: AppUpdate, db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, app_ref)
    dataset = await _dataset_for(db, row)

    if body.name is not None:
        name = body.name.strip()
        if not ds_svc.NAME_RE.match(name):
            raise HTTPException(status_code=400, detail="Invalid app name")
        if await app_svc.name_taken(db, name, exclude_id=row.id):
            raise HTTPException(status_code=409, detail=f"App {name!r} already exists")
        row.name = name
        row.slug = await app_svc.unique_slug(db, name, exclude_id=row.id)
    if body.description is not None:
        row.description = body.description.strip()
    if body.dataset_id is not None:
        if body.dataset_id == "":
            row.dataset_id = None
            dataset = None
        else:
            dataset = await db.get(Dataset, body.dataset_id)
            if dataset is None:
                raise HTTPException(status_code=404, detail="Dataset not found")
            row.dataset_id = dataset.id
    if body.config is not None:
        if row.status == "published":
            raise HTTPException(status_code=409, detail="Unpublish before editing the config")
        try:
            app_svc.validate_config(body.config, (dataset.schema_json if dataset else []) or [])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        row.config = body.config

    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _out(row, dataset)


@router.delete("/{app_ref}", status_code=204)
async def delete_app(app_ref: str, db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, app_ref)
    await db.delete(row)
    await db.commit()


@router.post("/{app_ref}/generate", response_model=AppOut)
async def regenerate_app(app_ref: str, db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, app_ref)
    dataset = await _dataset_for(db, row)
    if dataset is None:
        raise HTTPException(status_code=409, detail="Bind a dataset first")
    if row.status == "published":
        raise HTTPException(status_code=409, detail="Unpublish before regenerating")
    df = ds_svc.read_parquet_df(ds_svc.parquet_path(dataset.id))
    row.config = app_svc.generate_config(df, dataset.schema_json or [])
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _out(row, dataset)


@router.post("/{app_ref}/publish", response_model=AppOut)
async def publish_app(app_ref: str, db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, app_ref)
    dataset = await _dataset_for(db, row)
    if dataset is None:
        raise HTTPException(status_code=409, detail="Bind a dataset before publishing")
    try:
        app_svc.validate_config(row.config or {}, dataset.schema_json or [])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid config: {exc}") from exc
    row.status = "published"
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _out(row, dataset)


@router.post("/{app_ref}/unpublish", response_model=AppOut)
async def unpublish_app(app_ref: str, db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, app_ref)
    row.status = "draft"
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return await _out_with_dataset(db, row)


# ----------------------------------------------------------------- runtime
@router.get("/{slug}/runtime")
async def runtime(slug: str, db: AsyncSession = Depends(get_db)):
    row = await _runtime_or_404(db, slug)
    dataset = await _dataset_for(db, row)
    components = (row.config or {}).get("components", [])
    payload: dict = {
        "app": {
            "name": row.name,
            "slug": row.slug,
            "description": row.description or "",
            "config": row.config or {},
            "status": row.status,
        },
        "dataset": None,
        "stats": {},
        "chart": None,
    }
    if dataset is not None:
        df = ds_svc.read_parquet_df(ds_svc.parquet_path(dataset.id))
        payload["dataset"] = {
            "id": dataset.id,
            "name": dataset.name,
            "schema_json": dataset.schema_json or [],
            "row_count": dataset.row_count,
        }
        payload["stats"] = app_svc.compute_stats(components, df)
        payload["chart"] = app_svc.compute_chart(components, df)
    return payload


# ----------------------------------------------------------------- rules (v30)
@router.get("/{app_ref}/rules")
async def get_rules(app_ref: str, db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, app_ref)
    return {
        "rules": (row.config or {}).get("rules", []),
        "ops": sorted(rule_svc.RULE_OPS),
        "actions": sorted(rule_svc.RULE_ACTIONS),
        "events": sorted(rule_svc.RULE_EVENTS),
    }


@router.put("/{app_ref}/rules")
async def put_rules(app_ref: str, body: RulesPut, db: AsyncSession = Depends(get_db)):
    """Replace all rules — allowed on PUBLISHED apps too (layout stays locked)."""
    row = await _get_or_404(db, app_ref)
    dataset = await _dataset_for(db, row)
    if dataset is None:
        raise HTTPException(status_code=409, detail="Bind a dataset before adding rules")
    try:
        rule_svc.validate_rules(body.rules, dataset.schema_json or [])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row.config = {**(row.config or {}), "rules": body.rules}
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {"ok": True, "rules": body.rules}


@router.post("/{app_ref}/rules/test")
async def test_rules(app_ref: str, body: RulesTestIn, db: AsyncSession = Depends(get_db)):
    """Dry-run a sample record — which rules fire, what they would do."""
    row = await _get_or_404(db, app_ref)
    dataset = await _dataset_for(db, row)
    if dataset is None:
        raise HTTPException(status_code=409, detail="Bind a dataset before testing rules")
    return rule_svc.dry_run(
        (row.config or {}).get("rules", []),
        body.record,
        body.event if body.event in ("create", "update") else "create",
        dataset.schema_json or [],
    )


# ----------------------------------------------------------------- forms (v30)
@router.get("/{slug}/form")
async def form_descriptor(slug: str, db: AsyncSession = Depends(get_db)):
    """Standalone form descriptor for the public /f/{slug} page."""
    row = await _runtime_or_404(db, slug)
    form = _form_comp(row)
    if form is None:
        raise HTTPException(status_code=409, detail="App has no form component")
    dataset = await _dataset_for(db, row)
    return {
        "app": {"name": row.name, "slug": row.slug, "description": row.description or ""},
        "form": {
            "title": form.get("title", "Submit"),
            "submit_label": form.get("submit_label", "Submit"),
            "fields": app_svc.form_fields(form),
        },
        "dataset": {"name": dataset.name, "row_count": dataset.row_count} if dataset else None,
    }


@router.post("/{slug}/form-submit", status_code=201)
async def form_submit(slug: str, body: AppRecordIn, db: AsyncSession = Depends(get_db)):
    """Anonymous single-form submission — same pipeline as records POST."""
    row = await _runtime_or_404(db, slug)
    if _form_comp(row) is None:
        raise HTTPException(status_code=409, detail="App has no form component")
    dataset = await _dataset_for(db, row)
    if dataset is None:
        raise HTTPException(status_code=409, detail="App has no dataset bound")
    try:
        result = await app_svc.append_record(
            dataset, body.record, dataset.schema_json or [],
            form=_form_comp(row), rules=(row.config or {}).get("rules", []),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.add(dataset)
    await db.commit()
    await db.refresh(dataset)
    return {"ok": True, "row_count": dataset.row_count, "warnings": result["warnings"]}


@router.get("/{slug}/records")
async def runtime_records(
    slug: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
):
    row = await _runtime_or_404(db, slug)
    dataset = await _dataset_for(db, row)
    if dataset is None:
        return {"rows": [], "row_count": 0, "offset": offset, "limit": limit, "columns": []}
    df = ds_svc.read_parquet_df(ds_svc.parquet_path(dataset.id))
    page = df.iloc[offset : offset + limit]
    return {
        "rows": ds_svc.jsonable_rows(page),
        "row_count": int(len(df)),
        "offset": offset,
        "limit": limit,
        "columns": [c["name"] for c in (dataset.schema_json or [])],
    }


@router.post("/{slug}/records", status_code=201)
async def create_record(slug: str, body: AppRecordIn, db: AsyncSession = Depends(get_db)):
    row = await _runtime_or_404(db, slug)
    dataset = await _dataset_for(db, row)
    if dataset is None:
        raise HTTPException(status_code=409, detail="App has no dataset bound")
    try:
        result = await app_svc.append_record(
            dataset, body.record, dataset.schema_json or [],
            form=_form_comp(row), rules=(row.config or {}).get("rules", []),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.add(dataset)
    await db.commit()
    await db.refresh(dataset)
    return {"ok": True, "row_count": dataset.row_count, "warnings": result["warnings"]}


@router.patch("/{slug}/records/{index}")
async def edit_record(slug: str, index: int, body: AppRecordIn, db: AsyncSession = Depends(get_db)):
    row = await _runtime_or_404(db, slug)
    dataset = await _dataset_for(db, row)
    if dataset is None:
        raise HTTPException(status_code=409, detail="App has no dataset bound")
    try:
        result = await app_svc.update_record(
            dataset, index, body.record,
            form=_form_comp(row), rules=(row.config or {}).get("rules", []),
        )
    except IndexError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.add(dataset)
    await db.commit()
    await db.refresh(dataset)
    return {"ok": True, "record": result["record"], "row_count": dataset.row_count, "warnings": result["warnings"]}


@router.delete("/{slug}/records/{index}")
async def remove_record(slug: str, index: int, db: AsyncSession = Depends(get_db)):
    row = await _runtime_or_404(db, slug)
    dataset = await _dataset_for(db, row)
    if dataset is None:
        raise HTTPException(status_code=409, detail="App has no dataset bound")
    try:
        remaining = await app_svc.delete_record(dataset, index)
    except IndexError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.add(dataset)
    await db.commit()
    await db.refresh(dataset)
    return {"ok": True, "row_count": remaining}
