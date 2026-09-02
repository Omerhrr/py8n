"""Datasets API (v27) - first-class tabular data objects.

Endpoints
---------
GET    /datasets                 list metadata
POST   /datasets                 create from JSON rows
POST   /datasets/upload          create from an uploaded file (xlsx/csv/json)
POST   /datasets/query           run DuckDB SQL across ALL datasets (views)
GET    /datasets/{id}            metadata (by id or name)
GET    /datasets/{id}/rows       paginated rows
GET    /datasets/{id}/profile    per-column profiling stats
GET    /datasets/{id}/export     download as csv/xlsx/json/parquet (v45)
POST   /datasets/{id}/rows       append rows
PUT    /datasets/{id}            rename / re-describe
DELETE /datasets/{id}            drop metadata + parquet file

Rows are stored as Parquet via DuckDB; SQL runs against every dataset
registered as a view (name lowercased, non-alphanumerics folded to ``_``).
Every mutation commits explicitly (v4 lesson).
"""

from __future__ import annotations

import io
import json

import pandas as pd
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user, own_or_404, scope_rows
from ..db import get_db
from ..models import Dataset, DatasetVersion
from ..schemas import (
    DatasetCreate,
    DatasetOut,
    DatasetQueryIn,
    DatasetRowsIn,
    DatasetUpdate,
)
from ..services import datasets as ds_svc

router = APIRouter(prefix="/datasets", tags=["datasets"])

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB


def _out(row: Dataset) -> DatasetOut:
    return DatasetOut(
        id=row.id,
        name=row.name,
        description=row.description or "",
        schema_json=row.schema_json or [],
        row_count=row.row_count,
        source=row.source,
        tags=row.tags or [],
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _get_or_404(db: AsyncSession, dataset_id: str, user=None) -> Dataset:
    row = await ds_svc.get_dataset(db, dataset_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    own_or_404(row.owner_id, user)  # v37
    return row


def _parse_upload(filename: str, raw: bytes) -> pd.DataFrame:
    """File bytes → normalized DataFrame (xlsx/csv/json)."""
    lower = (filename or "").lower()
    try:
        if lower.endswith((".xlsx", ".xls")):
            df = pd.read_excel(io.BytesIO(raw), sheet_name=0)
        elif lower.endswith(".csv") or lower.endswith(".txt"):
            try:
                df = pd.read_csv(io.BytesIO(raw))
            except UnicodeDecodeError:
                df = pd.read_csv(io.BytesIO(raw), encoding="latin-1")
        elif lower.endswith(".json"):
            payload = json.loads(raw.decode("utf-8"))
            if isinstance(payload, list):
                df = pd.DataFrame(payload)
            elif isinstance(payload, dict):
                df = pd.DataFrame([payload])
            else:
                raise ValueError("JSON must be a list of objects or a single object")
        else:
            raise HTTPException(
                status_code=415,
                detail="Unsupported file type - upload .xlsx, .csv or .json",
            )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - parse failures surface as 400
        raise HTTPException(status_code=400, detail=f"Could not parse file: {exc}") from exc
    if df.empty:
        raise HTTPException(status_code=400, detail="File contains no data rows")
    return ds_svc.normalize_df(df)


@router.get("", response_model=list[DatasetOut])
async def list_datasets(
    tag: str | None = Query(default=None, description="filter by tag (case-insensitive)"),
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(select(Dataset).order_by(Dataset.updated_at.desc()))).scalars().all()
    visible = scope_rows(rows, user)  # v37
    if tag:
        want = tag.lower()
        visible = [d for d in visible if any(t.lower() == want for t in (d.tags or []))]
    return [_out(r) for r in visible]


@router.post("", response_model=DatasetOut, status_code=201)
async def create_dataset(body: DatasetCreate, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    name = body.name.strip()
    if not ds_svc.NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail="Name must start with a letter or digit and contain only letters, digits, spaces, dots, dashes or underscores",
        )
    if await ds_svc.name_taken(db, name):
        raise HTTPException(status_code=409, detail=f"Dataset {name!r} already exists")
    df = ds_svc.normalize_df(pd.DataFrame(body.rows)) if body.rows else pd.DataFrame()
    row = await ds_svc.create_from_df(db, name, df, source="api", description=body.description,
                                      owner_id=user.id if user else None)
    if body.tags:
        cleaned: list[str] = []
        seen: set[str] = set()
        for t in body.tags:
            if not isinstance(t, str):
                continue
            t = t.strip()
            if t and t.lower() not in seen:
                seen.add(t.lower())
                cleaned.append(t)
        row.tags = cleaned
    await db.commit()
    await db.refresh(row)
    return _out(row)


@router.post("/upload", response_model=DatasetOut, status_code=201)
async def upload_dataset(
    file: UploadFile = File(...),
    name: str = Form(...),
    description: str = Form(""),
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    name = (name or "").strip() or (file.filename or "").rsplit(".", 1)[0].strip()
    if not name:
        raise HTTPException(status_code=400, detail="A dataset name is required")
    if not ds_svc.NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid dataset name")
    if await ds_svc.name_taken(db, name):
        raise HTTPException(status_code=409, detail=f"Dataset {name!r} already exists")
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 25 MB)")
    df = _parse_upload(file.filename or name, raw)
    row = await ds_svc.create_from_df(db, name, df, source="upload", description=description,
                                      owner_id=user.id if user else None)
    await db.commit()
    await db.refresh(row)
    return _out(row)


@router.post("/query")
async def query_datasets(body: DatasetQueryIn, db: AsyncSession = Depends(get_db)):
    try:
        return await ds_svc.run_sql(db, body.sql)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{dataset_id}", response_model=DatasetOut)
async def get_dataset(dataset_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    return _out(await _get_or_404(db, dataset_id, user))


@router.get("/{dataset_id}/rows")
async def get_rows(
    dataset_id: str,
    offset: int = 0,
    limit: int = 100,
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    row = await _get_or_404(db, dataset_id, user)
    offset = max(0, offset)
    limit = min(max(1, limit), 1000)
    df = ds_svc.read_parquet_df(ds_svc.parquet_path(row.id))
    page = df.iloc[offset : offset + limit]
    return {
        "rows": ds_svc.jsonable_rows(page),
        "row_count": row.row_count,
        "offset": offset,
        "limit": limit,
        "columns": [c["name"] for c in (row.schema_json or [])],
    }


@router.get("/{dataset_id}/profile")
async def get_profile(dataset_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, dataset_id, user)
    df = ds_svc.read_parquet_df(ds_svc.parquet_path(row.id))
    return ds_svc.profile_df(df)


@router.get("/{dataset_id}/export")
async def export_dataset(
    dataset_id: str,
    fmt: str = Query("csv", description="csv|xlsx|json|parquet"),
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """Download the dataset as a file (v45) - owner-scoped, size-capped."""
    row = await _get_or_404(db, dataset_id, user)
    try:
        data, content_type, ext = ds_svc.export_dataset_bytes(row, fmt)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    filename = f"{ds_svc.view_name(row.name)}.{ext}"
    return Response(
        content=data,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{dataset_id}/rows", response_model=DatasetOut)
async def append_rows(dataset_id: str, body: DatasetRowsIn, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, dataset_id, user)
    fresh = ds_svc.normalize_df(pd.DataFrame(body.rows))
    await ds_svc.append_rows(db, row, fresh.to_dict(orient="records"))
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _out(row)


@router.put("/{dataset_id}", response_model=DatasetOut)
async def update_dataset(dataset_id: str, body: DatasetUpdate, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, dataset_id, user)
    if body.name is not None:
        name = body.name.strip()
        if not ds_svc.NAME_RE.match(name):
            raise HTTPException(status_code=400, detail="Invalid dataset name")
        if await ds_svc.name_taken(db, name, exclude_id=row.id):
            raise HTTPException(status_code=409, detail=f"Dataset {name!r} already exists")
        row.name = name
    if body.description is not None:
        row.description = body.description.strip()
    if body.tags is not None:  # v44: omitted = untouched; [] clears all
        row.tags = body.tags or []
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _out(row)


@router.delete("/{dataset_id}", status_code=204)
async def delete_dataset(dataset_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, dataset_id, user)
    ds_svc.delete_file(row)
    ds_svc.delete_versions(row.id)  # v44: snapshots die with the dataset
    await db.delete(row)
    await db.commit()


# ------------------------------------------------------------------ versions (v44)
def _version_out(v: DatasetVersion) -> dict:
    return {
        "id": v.id,
        "dataset_id": v.dataset_id,
        "version": v.version,
        "row_count": v.row_count,
        "source": v.source,
        "note": v.note,
        "created_at": v.created_at,
        "current": False,
        "file_exists": ds_svc.version_file(v.dataset_id, v.version).exists(),
    }


@router.get("/{dataset_id}/versions")
async def list_versions(dataset_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Version timeline, newest first. The newest entry is always the CURRENT
    state; restoring any entry records the restored content as a new version."""
    row = await _get_or_404(db, dataset_id, user)
    rows = (
        await db.execute(
            select(DatasetVersion)
            .where(DatasetVersion.dataset_id == row.id)
            .order_by(DatasetVersion.version.desc())
        )
    ).scalars().all()
    out = [_version_out(v) for v in rows]
    if out:
        out[0]["current"] = True
    return out


@router.get("/{dataset_id}/lineage")
async def dataset_lineage(dataset_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """v47: provenance timeline - every version with the workflow/execution/
    node that produced it (NULL provenance = API/upload-side write). Workflow
    names are resolved so the UI can show 'Ledger ETL v12' instead of a uuid."""
    row = await _get_or_404(db, dataset_id, user)
    versions = (
        await db.execute(
            select(DatasetVersion)
            .where(DatasetVersion.dataset_id == row.id)
            .order_by(DatasetVersion.version.asc())
        )
    ).scalars().all()
    wf_ids = {v.workflow_id for v in versions if v.workflow_id}
    wf_names: dict[str, str] = {}
    if wf_ids:
        from ..models import Workflow

        wf_rows = (
            await db.execute(select(Workflow.id, Workflow.name).where(Workflow.id.in_(wf_ids)))
        ).all()
        wf_names = {wid: name for wid, name in wf_rows}
    steps = [
        {
            "version": v.version,
            "row_count": v.row_count,
            "source": v.source,
            "note": v.note,
            "created_at": v.created_at,
            "workflow_id": v.workflow_id,
            "workflow_name": wf_names.get(v.workflow_id) if v.workflow_id else None,
            "execution_id": v.execution_id,
            "node_name": v.node_name,
            "origin": "workflow" if v.workflow_id else "surface",
        }
        for v in versions
    ]
    return {
        "dataset_id": row.id,
        "name": row.name,
        "created_at": row.created_at,
        "row_count": row.row_count,
        "workflow_versions": sum(1 for s in steps if s["origin"] == "workflow"),
        "steps": steps,
    }


@router.get("/{dataset_id}/versions/{version}/rows")
async def version_rows(
    dataset_id: str,
    version: int,
    limit: int = Query(default=50, ge=1, le=1000),
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """Preview rows stored in a snapshot (no restore needed to look)."""
    await _get_or_404(db, dataset_id, user)
    f = ds_svc.version_file(dataset_id, version)
    if not f.exists():
        raise HTTPException(status_code=404, detail="Snapshot file not found (pruned or fileless)")
    df = ds_svc.read_parquet_df(f).head(limit)
    return {
        "version": version,
        "columns": list(df.columns),
        "rows": ds_svc.jsonable_rows(df),
        "shown": int(len(df)),
    }


@router.post("/{dataset_id}/versions/{version}/restore", response_model=DatasetOut)
async def restore_dataset_version(
    dataset_id: str, version: int, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)
):
    """Roll the dataset back to a snapshot. The restored content is recorded
    as a NEW version (source=restore), so the operation is undoable."""
    row = await _get_or_404(db, dataset_id, user)
    try:
        await ds_svc.restore_version(db, row, version)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _out(row)


@router.delete("/{dataset_id}/versions/{version}", status_code=204)
async def delete_dataset_version(
    dataset_id: str, version: int, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)
):
    """Drop one snapshot (row + file). The CURRENT version can be deleted
    too - it just stops being a restore point, the live dataset is untouched."""
    row = await _get_or_404(db, dataset_id, user)
    v = (
        await db.execute(
            select(DatasetVersion).where(
                DatasetVersion.dataset_id == row.id, DatasetVersion.version == version
            )
        )
    ).scalar_one_or_none()
    if v is None:
        raise HTTPException(status_code=404, detail="Version not found")
    f = ds_svc.version_file(row.id, version)
    if f.exists():
        f.unlink()
    await db.delete(v)
    await db.commit()
    return None
