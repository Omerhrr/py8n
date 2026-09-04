"""Model Deployments API (v67, deepened in v68) - the DEPLOY verb first-class.

* ``POST   /deployments``                - deploy a registry row: py8n generates the
  serving workflow (webhook -> lm_generate | split_out -> model_predict),
  activates it, and returns the live endpoint handle + request shape
* ``GET    /deployments``                - cards with derived 7d serving stats
* ``GET    /deployments/{id}``           - one deployment, stats included
* ``POST   /deployments/{id}/toggle``    - disable/enable (the workflow follows)
* ``DELETE /deployments/{id}``           - retire (the workflow survives, deactivated)

v68 - operating a live endpoint:

* ``POST   /deployments/{id}/tokens``    - mint a serving token (shown ONCE);
  a deployment with >=1 active token demands it on every call
* ``GET    /deployments/{id}/tokens``    - list tokens (masked, with last_used)
* ``DELETE /deployments/{id}/tokens/{token_id}`` - revoke
* ``GET    /deployments/{id}/versions``  - revision ledger + registry versions
* ``POST   /deployments/{id}/redeploy``  - same URL, new registry row
* ``POST   /deployments/{id}/rollback``  - re-activate an older revision/version
* ``POST   /deployments/{id}/stream``    - SSE generation (meta -> token* -> done)

The HTTP contract once deployed: ``POST /api/v1/webhooks/{workflow_id}``
with ``{"prompt": "..."}`` for language models or ``{"rows": [...]}`` for
the sklearn/neural surface - the response carries the final node's output
(the generated text / the scored rows). When the deployment holds serving
tokens every call must carry one; the stream endpoint enforces the same.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user
from ..db import get_db
from ..models import ModelDeployment
from ..services import deployments as dep_svc
from ..services import serving_limits

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


# ---------------------------------------------------------------------------
# v68: serving tokens
# ---------------------------------------------------------------------------

class TokenCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    rate_per_min: int | None = Field(default=None, ge=1, description="v69: max requests/minute (sliding window); null = unlimited")
    daily_quota: int | None = Field(default=None, ge=1, description="v69: max requests per UTC day; null = unlimited")


async def _own_deployment(db: AsyncSession, deployment_id: str, user) -> ModelDeployment:
    row = await db.get(ModelDeployment, deployment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    if user is not None and row.owner_id is not None and row.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Not found")
    return row


@router.post("/{deployment_id}/tokens", status_code=201)
async def mint_token(deployment_id: str, body: TokenCreate,
                     user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    await _own_deployment(db, deployment_id, user)
    try:
        return await dep_svc.mint_token(db, owner_id=getattr(user, "id", None),
                                        deployment_id=deployment_id, name=body.name,
                                        rate_per_min=body.rate_per_min,
                                        daily_quota=body.daily_quota)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class TokenLimits(BaseModel):
    rate_per_min: int | None = Field(default=None, ge=1, description="max requests/minute; null = unlimited")
    daily_quota: int | None = Field(default=None, ge=1, description="max requests/UTC day; null = unlimited")


@router.put("/{deployment_id}/tokens/{token_id}/limits")
async def set_token_limits(deployment_id: str, token_id: str, body: TokenLimits,
                           user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Upsert a token's rate-shaping/quotas (v69). Clears a limit with null."""
    await _own_deployment(db, deployment_id, user)
    try:
        out = await dep_svc.set_token_limits(db, deployment_id, token_id,
                                             body.rate_per_min, body.daily_quota)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if out is None:
        raise HTTPException(status_code=404, detail="Not found")
    return out


@router.get("/{deployment_id}/tokens/{token_id}/usage")
async def token_usage(deployment_id: str, token_id: str,
                      user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """The token's policy + live in-process usage counters (v69)."""
    await _own_deployment(db, deployment_id, user)
    out = await dep_svc.get_token_usage(db, deployment_id, token_id)
    if out is None:
        raise HTTPException(status_code=404, detail="Not found")
    return out


@router.get("/{deployment_id}/tokens")
async def list_tokens(deployment_id: str, user=Depends(get_optional_user),
                      db: AsyncSession = Depends(get_db)):
    await _own_deployment(db, deployment_id, user)
    return await dep_svc.list_tokens(db, deployment_id)


@router.delete("/{deployment_id}/tokens/{token_id}")
async def revoke_token(deployment_id: str, token_id: str,
                       user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    await _own_deployment(db, deployment_id, user)
    out = await dep_svc.revoke_token(db, deployment_id, token_id)
    if out is None:
        raise HTTPException(status_code=404, detail="Not found")
    return out


# ---------------------------------------------------------------------------
# v68: redeploy / rollback / versions
# ---------------------------------------------------------------------------

class RedeployBody(BaseModel):
    model: str = Field(..., min_length=1, description="Registry name+version target (ACTIVE) or registry row id")
    note: str = Field(default="", max_length=500)


class RollbackBody(BaseModel):
    revision: int | None = Field(default=None, ge=1, description="Ledger revision to re-activate")
    version: int | None = Field(default=None, ge=1, description="Registry version of the deployment's model name to roll back to")
    note: str = Field(default="", max_length=500)


@router.get("/{deployment_id}/versions")
async def deployment_versions(deployment_id: str, user=Depends(get_optional_user),
                              db: AsyncSession = Depends(get_db)):
    row = await _own_deployment(db, deployment_id, user)
    return await dep_svc.list_deployment_versions(db, row, getattr(user, "id", None))


@router.post("/{deployment_id}/redeploy")
async def redeploy(deployment_id: str, body: RedeployBody,
                   user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await _own_deployment(db, deployment_id, user)
    try:
        return await dep_svc.redeploy_deployment(db, row, owner_id=getattr(user, "id", None),
                                                 model_ref=body.model, note=body.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{deployment_id}/rollback")
async def rollback(deployment_id: str, body: RollbackBody,
                   user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await _own_deployment(db, deployment_id, user)
    try:
        return await dep_svc.rollback_deployment(db, row, owner_id=getattr(user, "id", None),
                                                 revision=body.revision, version=body.version,
                                                 note=body.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# v68: SSE streaming generation
# ---------------------------------------------------------------------------

class StreamBody(BaseModel):
    prompt: str = Field(default="", max_length=8000)
    max_tokens: int = Field(default=32, ge=1, le=512)
    temperature: float = Field(default=0.8, gt=0, le=2)
    top_k: int = Field(default=40, ge=0)
    seed: int = Field(default=42)


@router.post("/{deployment_id}/stream")
async def stream_generation(deployment_id: str, body: StreamBody, request: Request,
                            user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await _own_deployment(db, deployment_id, user)
    # Serving tokens gate the stream exactly like the webhook endpoint -
    # and the matched token's rate-shaping/quotas apply (v69).
    try:
        token = await dep_svc.check_deployment_token(db, row, request)
        await dep_svc.enforce_serving_limits(token)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except serving_limits.LimitExceeded as exc:
        raise HTTPException(status_code=429, detail=exc.detail,
                            headers=exc.headers) from exc
    return StreamingResponse(
        dep_svc.stream_generation(db, row, owner_id=getattr(user, "id", None),
                                  prompt=body.prompt, max_tokens=body.max_tokens,
                                  temperature=body.temperature, top_k=body.top_k,
                                  seed=body.seed),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
