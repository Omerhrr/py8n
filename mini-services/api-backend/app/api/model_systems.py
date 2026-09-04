"""Model Systems API (v63) - the AI model-building operating unit.

* ``POST   /model-systems``                     - create one
* ``GET    /model-systems``                     - cards with counts + verdict
* ``GET    /model-systems/capabilities``        - the honest modality matrix
* ``GET    /model-systems/{id}``                - the nine derived sections
* ``PUT    /model-systems/{id}``                - rename / restyle / declare modalities
* ``POST   /model-systems/{id}/components``     - bind dataset|model|workflow|report
* ``DELETE /model-systems/{id}/components/{cid}`` - unbind
* ``DELETE /model-systems/{id}``                - dissolve (members untouched)
* ``GET    /model-systems/{id}/lifecycle``      - derived LM stage plan (v65)
* ``POST   /model-systems/{id}/run-lifecycle``  - run pretrain->continue->generate in sequence (v65)

Training happens in WORKFLOWS (model_train / neural_train nodes); the
model system is the derived cockpit over what those runs produce.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..auth import get_optional_user, own_or_404
from ..db import get_db
from ..models import ModelSystem, ModelSystemComponent
from ..services.model_systems import (
    COMPONENT_KINDS,
    KIND_TABLES,
    CAPABILITIES,
    derive_lifecycle,
    model_system_detail,
    model_system_health,
    model_system_summary,
    resolve_component,
    run_lifecycle,
)

router = APIRouter(prefix="/model-systems", tags=["model-systems"])

KNOWN_MODALITIES = ("tabular", "text", "image", "audio", "video", "document", "multimodal")


class ModelSystemCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=140)
    description: str = Field(default="", max_length=2000)
    icon: str = Field(default="brain-circuit", max_length=60)
    color: str = Field(default="#818cf8", max_length=20)
    modalities: list[str] = Field(default_factory=list, description=f"subset of: {', '.join(KNOWN_MODALITIES)}")


class ModelSystemUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=140)
    description: str | None = Field(default=None, max_length=2000)
    icon: str | None = Field(default=None, max_length=60)
    color: str | None = Field(default=None, max_length=20)
    modalities: list[str] | None = None


class ComponentAttach(BaseModel):
    kind: str = Field(..., description=f"one of: {', '.join(COMPONENT_KINDS)}")
    ref_id: str = Field(..., min_length=1, max_length=36)


class LifecycleRunRequest(BaseModel):
    timeout_s: int | None = Field(default=None, ge=10, le=900,
                                  description="Per-stage wait budget (default 240s, clamped 10-900)")


async def _get_ms(db: AsyncSession, ms_id: str, user) -> ModelSystem:
    row = (
        await db.execute(
            select(ModelSystem).options(selectinload(ModelSystem.components)).where(ModelSystem.id == ms_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Model system not found")
    own_or_404(row.owner_id, user)
    return row


async def _reload(db: AsyncSession, ms_id: str) -> ModelSystem:
    return (
        await db.execute(
            select(ModelSystem).options(selectinload(ModelSystem.components)).where(ModelSystem.id == ms_id)
        )
    ).scalar_one()


@router.post("", status_code=201)
async def create_model_system(body: ModelSystemCreate, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    unknown = [m for m in body.modalities if m not in KNOWN_MODALITIES]
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown modality {unknown[0]!r} (allowed: {', '.join(KNOWN_MODALITIES)})")
    row = ModelSystem(name=body.name.strip(), description=body.description.strip(),
                      icon=body.icon, color=body.color, modalities=list(dict.fromkeys(body.modalities)))
    row.owner_id = user.id if user else None
    db.add(row)
    await db.commit()
    return model_system_summary(await _reload(db, row.id))


@router.get("")
async def list_model_systems(user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    q = (
        select(ModelSystem)
        .options(selectinload(ModelSystem.components))
        .order_by(ModelSystem.updated_at.desc())
        .limit(100)
    )
    rows = (await db.execute(q)).scalars().unique().all()
    uid = getattr(user, "id", None)
    out = []
    for s in rows:
        if uid and s.owner_id not in (uid, None):
            continue
        summary = model_system_summary(s)
        summary["verdict"] = (await model_system_health(db, s))["verdict"]
        out.append(summary)
    return out


@router.get("/capabilities")
async def capabilities():
    """The honest modality matrix - what this build can extract today."""
    return {"capabilities": CAPABILITIES,
            "note": "availability reflects inline-mode extraction (v65: video frame sampling via OpenCV is available)"}


@router.get("/{ms_id}/lifecycle")
async def lifecycle(ms_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """v65: the derived LM lifecycle plan (pretrain -> continue -> generate)
    read off the bound workflows' graphs - nothing stored, nothing run."""
    ms = await _get_ms(db, ms_id, user)
    return await derive_lifecycle(db, ms)


@router.post("/{ms_id}/run-lifecycle")
async def run_lifecycle_endpoint(ms_id: str, body: LifecycleRunRequest | None = None,
                                 user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """v65: run the full LM lifecycle IN SEQUENCE through the real engine.

    Each bound LM workflow (pretrain, then continue stages, then generate)
    is dispatched and awaited; the sequence stops at the first failure.
    """
    ms = await _get_ms(db, ms_id, user)
    return await run_lifecycle(db, ms, (body or LifecycleRunRequest()).timeout_s)


@router.get("/{ms_id}")
async def detail(ms_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    ms = await _get_ms(db, ms_id, user)
    return await model_system_detail(db, ms)


@router.put("/{ms_id}")
async def update(ms_id: str, body: ModelSystemUpdate, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    ms = await _get_ms(db, ms_id, user)
    if body.name is not None:
        ms.name = body.name.strip()
    if body.description is not None:
        ms.description = body.description.strip()
    if body.icon is not None:
        ms.icon = body.icon
    if body.color is not None:
        ms.color = body.color
    if body.modalities is not None:
        unknown = [m for m in body.modalities if m not in KNOWN_MODALITIES]
        if unknown:
            raise HTTPException(status_code=400, detail=f"unknown modality {unknown[0]!r} (allowed: {', '.join(KNOWN_MODALITIES)})")
        ms.modalities = list(dict.fromkeys(body.modalities))
    await db.commit()
    return model_system_summary(await _reload(db, ms.id))


@router.post("/{ms_id}/components", status_code=201)
async def attach(ms_id: str, body: ComponentAttach, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    ms = await _get_ms(db, ms_id, user)
    scope_owner = ms.owner_id or (user.id if user else None)
    try:
        await resolve_component(db, body.kind, body.ref_id, scope_owner)
    except ValueError as exc:
        raise HTTPException(status_code=404 if "not found" in str(exc) else 400, detail=str(exc)) from exc
    dup = (
        await db.execute(
            select(ModelSystemComponent).where(
                ModelSystemComponent.model_system_id == ms.id,
                ModelSystemComponent.kind == body.kind,
                ModelSystemComponent.ref_id == body.ref_id,
            )
        )
    ).scalar_one_or_none()
    if dup:
        raise HTTPException(status_code=409, detail="that object is already bound to this model system")
    comp = ModelSystemComponent(model_system_id=ms.id, kind=body.kind, ref_id=body.ref_id)
    db.add(comp)
    await db.commit()
    await db.refresh(comp)
    fresh = model_system_summary(await _reload(db, ms.id))
    return {"component_id": comp.id, "kind": comp.kind, "ref_id": comp.ref_id,
            "system_total": fresh["total_components"]}


@router.delete("/{ms_id}/components/{component_id}", status_code=204)
async def detach(ms_id: str, component_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    ms = await _get_ms(db, ms_id, user)
    comp = await db.get(ModelSystemComponent, component_id)
    if comp is None or comp.model_system_id != ms.id:
        raise HTTPException(status_code=404, detail="Component not found")
    await db.delete(comp)
    await db.commit()


@router.delete("/{ms_id}", status_code=204)
async def dissolve(ms_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    ms = await _get_ms(db, ms_id, user)
    await db.delete(ms)  # components cascade; member objects are untouched
    await db.commit()
