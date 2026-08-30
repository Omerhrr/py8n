"""Datasets API (v27) — first-class tabular data objects.

Endpoints
---------
GET    /datasets                 list metadata
POST   /datasets                 create from JSON rows
POST   /datasets/upload          create from an uploaded file (xlsx/csv/json)
POST   /datasets/query           run DuckDB SQL across ALL datasets (views)
GET    /datasets/{id}            metadata (by id or name)
GET    /datasets/{id}/rows       paginated rows
GET    /datasets/{id}/profile    per-column profiling stats
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
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import Dataset
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
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _get_or_404(db: AsyncSession, dataset_id: str) -> Dataset:
    row = await ds_svc.get_dataset(db, dataset_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
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
                detail="Unsupported file type — upload .xlsx, .csv or .json",
            )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — parse failures surface as 400
        raise HTTPException(status_code=400, detail=f"Could not parse file: {exc}") from exc
    if df.empty:
        raise HTTPException(status_code=400, detail="File contains no data rows")
    return ds_svc.normalize_df(df)


@router.get("", response_model=list[DatasetOut])
async def list_datasets(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Dataset).order_by(Dataset.updated_at.desc()))).scalars().all()
    return [_out(r) for r in rows]


@router.post("", response_model=DatasetOut, status_code=201)
async def create_dataset(body: DatasetCreate, db: AsyncSession = Depends(get_db)):
    name = body.name.strip()
    if not ds_svc.NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail="Name must start with a letter or digit and contain only letters, digits, spaces, dots, dashes or underscores",
        )
    if await ds_svc.name_taken(db, name):
        raise HTTPException(status_code=409, detail=f"Dataset {name!r} already exists")
    df = ds_svc.normalize_df(pd.DataFrame(body.rows)) if body.rows else pd.DataFrame()
    row = await ds_svc.create_from_df(db, name, df, source="api", description=body.description)
    await db.commit()
    await db.refresh(row)
    return _out(row)


@router.post("/upload", response_model=DatasetOut, status_code=201)
async def upload_dataset(
    file: UploadFile = File(...),
    name: str = Form(...),
    description: str = Form(""),
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
    row = await ds_svc.create_from_df(db, name, df, source="upload", description=description)
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
async def get_dataset(dataset_id: str, db: AsyncSession = Depends(get_db)):
    return _out(await _get_or_404(db, dataset_id))


@router.get("/{dataset_id}/rows")
async def get_rows(
    dataset_id: str,
    offset: int = 0,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
):
    row = await _get_or_404(db, dataset_id)
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
async def get_profile(dataset_id: str, db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, dataset_id)
    df = ds_svc.read_parquet_df(ds_svc.parquet_path(row.id))
    return ds_svc.profile_df(df)


@router.post("/{dataset_id}/rows", response_model=DatasetOut)
async def append_rows(dataset_id: str, body: DatasetRowsIn, db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, dataset_id)
    fresh = ds_svc.normalize_df(pd.DataFrame(body.rows))
    await ds_svc.append_rows(row, fresh.to_dict(orient="records"))
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _out(row)


@router.put("/{dataset_id}", response_model=DatasetOut)
async def update_dataset(dataset_id: str, body: DatasetUpdate, db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, dataset_id)
    if body.name is not None:
        name = body.name.strip()
        if not ds_svc.NAME_RE.match(name):
            raise HTTPException(status_code=400, detail="Invalid dataset name")
        if await ds_svc.name_taken(db, name, exclude_id=row.id):
            raise HTTPException(status_code=409, detail=f"Dataset {name!r} already exists")
        row.name = name
    if body.description is not None:
        row.description = body.description.strip()
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _out(row)


@router.delete("/{dataset_id}", status_code=204)
async def delete_dataset(dataset_id: str, db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, dataset_id)
    ds_svc.delete_file(row)
    await db.delete(row)
    await db.commit()
