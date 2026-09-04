"""Model Deployments API (v67) - the DEPLOY verb made first-class.

* ``POST   /deployments``          - deploy a registry row: py8n generates the
  serving workflow (webhook -> lm_generate | split_out -> model_predict),
  activates it, and returns the live endpoint handle + request shape
* ``GET    /deployments``          - cards with derived 7d serving stats
* ``GET    /deployments/{id}``     - one deployment, stats included
* ``POST   /deployments/{id}/toggle`` - disable/enable (the workflow follows)
* ``DELETE /deployments/{id}``     - retire (the workflow survives, deactivated)

The HTTP contract once deployed: ``POST /api/v1/webhooks/{workflow_id}``
with ``{"prompt": "..."}`` for language models or ``{"rows": [...]}`` for
the sklearn/neural surface - the response carries the final node's output
(the generated text / the scored rows).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user
from ..db import get_db
from ..models import ModelDeployment
from ..services import deployments as dep_svc

router = APIRouter(prefix="/deployments", tags=["deployments"])


class DeploymentCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=140)
    model: str = Field(..., min_length=1, description="Registry name (ACTIVE version) or registry row id - deployments PIN the resolved version")
    environment: str = Field(default="dev", description="dev | staging | prod")
    notes: str = Field(default="", max_length=500)
    max_tokens: int | None = Field(default=None, ge=1, le=512, description="Language models only: generation cap (default 16)")
    temperature: float | None = Field(default=None, gt=0, le=2, description="Language models only: sampling temperature (default 0.8)")


@router.post("", status_code=201)
async def create_deployment(body: DeploymentCreate, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    generate_params: dict = {}
    if body.max_tokens is not None:
        generate_params["max_tokens"] = body.max_tokens
    if body.temperature is not None:
        generate_params["temperature"] = body.temperature
    try:
        return await dep_svc.create_deployment(
            db, owner_id=getattr(user, "id", None), name=body.name,
            model_ref=body.model, environment=body.environment,
            notes=body.notes, generate_params=generate_params)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("")
async def list_deployments(user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    return {"deployments": await dep_svc.list_deployments(db, getattr(user, "id", None))}


@router.get("/{deployment_id}")
async def get_deployment(deployment_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    out = await dep_svc.get_deployment(db, deployment_id, getattr(user, "id", None))
    if out is None:
        raise HTTPException(status_code=404, detail="Not found")
    return out


@router.post("/{deployment_id}/toggle")
async def toggle_deployment(deployment_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await db.get(ModelDeployment, deployment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    if user is not None and row.owner_id is not None and row.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Not found")
    return await dep_svc.toggle_deployment(db, row)


@router.delete("/{deployment_id}")
async def delete_deployment(deployment_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await db.get(ModelDeployment, deployment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    if user is not None and row.owner_id is not None and row.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Not found")
    return await dep_svc.delete_deployment(db, row)
