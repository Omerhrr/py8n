"""Models API (v46) - the ML model registry surface.

GET    /models            list registry rows (owner-scoped, name+version grouped)
GET    /models/{id}       one model (full metrics + features)
POST   /models/{id}/activate   make this version the active one for its name
DELETE /models/{id}       drop the registry row (+ artifact pickle)

Training registers models (model_train node, register=true); this router
manages the resulting registry. Every mutation commits explicitly (v4 lesson).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user, own_or_404, scope_rows
from ..db import get_db
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


@router.get("/{model_id}")
async def get_model(model_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    return model_svc.model_out(await _get_or_404(db, model_id, user))


@router.post("/{model_id}/activate")
async def activate_model(model_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, model_id, user)
    if row.active:
        return model_svc.model_out(row)
    await model_svc.activate_version(db, row)
    await db.commit()
    await db.refresh(row)
    return model_svc.model_out(row)


@router.delete("/{model_id}", status_code=204)
async def delete_model(model_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, model_id, user)
    await model_svc.delete_model(db, row)
    await db.commit()
