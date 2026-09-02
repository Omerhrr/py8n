"""Models API (v46) - the ML model registry surface.

GET    /models            list registry rows (owner-scoped, name+version grouped)
GET    /models/{id}       one model (full metrics + features)
POST   /models/{id}/activate   make this version the active one for its name
GET    /models/{ref}/drift?dataset_id=   PSI-score a dataset vs training stats (v47)
DELETE /models/{id}       drop the registry row (+ artifact pickle)

Training registers models (model_train node, register=true); this router
manages the resulting registry. Every mutation commits explicitly (v4 lesson).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user, own_or_404, scope_rows
from ..db import get_db
from ..services import datasets as ds_svc
from ..services import models as model_svc

router = APIRouter(prefix="/models", tags=["models"])


async def _get_or_404(db: AsyncSession, model_id: str, user=None):
    row = await model_svc.get_model(db, model_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Model not found")
    own_or_404(row.owner_id, user)
    return row


@router.get("")
async def list_models(user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    rows = await model_svc.list_models(db)
    return [model_svc.model_out(r) for r in scope_rows(rows, user)]


@router.get("/{model_ref}")
async def get_model(model_ref: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Resolve by registry id OR by name (name → the ACTIVE version)."""
    row = await model_svc.resolve_model(db, model_ref, owner_id=getattr(user, "id", None))
    if row is None:
        raise HTTPException(status_code=404, detail="Model not found")
    own_or_404(row.owner_id, user)
    return model_svc.model_out(row)


@router.post("/{model_id}/activate")
async def activate_model(model_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, model_id, user)
    if row.active:
        return model_svc.model_out(row)
    await model_svc.activate_version(db, row)
    await db.commit()
    await db.refresh(row)
    return model_svc.model_out(row)


@router.get("/{model_ref}/drift")
async def drift_check(
    model_ref: str,
    dataset_id: str,
    threshold: float = 0.25,
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """PSI-score a dataset against a model's training reference stats.

    ``model_ref`` is a registry id or name (resolves to the ACTIVE version);
    ``dataset_id`` is owner-scoped like every other dataset read. No
    reference stats (pre-v47 rows) -> 409 so the UI can explain why.
    """
    row = await model_svc.resolve_model(db, model_ref, owner_id=getattr(user, "id", None))
    if row is None:
        raise HTTPException(status_code=404, detail="Model not found")
    reference = row.reference_stats or {}
    if not reference:
        raise HTTPException(status_code=409, detail="Model has no reference stats - retrain with model_train v47+ to capture them")
    ds = await ds_svc.get_dataset(db, dataset_id, owner_id=getattr(user, "id", None))
    if ds is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    df = ds_svc.read_parquet_df(ds_svc.parquet_path(ds.id))
    if df is None or df.empty:
        raise HTTPException(status_code=409, detail="Dataset is empty - nothing to score")
    report = model_svc.score_drift(reference, df, row.features or [], threshold=threshold)
    report["model"] = {"id": row.id, "name": row.name, "version": row.version}
    report["dataset"] = {"id": ds.id, "name": ds.name, "rows": int(len(df))}
    return report


@router.delete("/{model_id}", status_code=204)
async def delete_model(model_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, model_id, user)
    await model_svc.delete_model(db, row)
    await db.commit()
