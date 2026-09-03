"""Py8n Systems API (v61) - the operating unit above workflows.

* ``POST   /systems``                        - create a system
* ``GET    /systems``                        - cards with component counts + verdict
* ``GET    /systems/{id}``                   - detail: grouped components + health
* ``PUT    /systems/{id}``                   - rename / redescribe / restyle
* ``POST   /systems/{id}/components``        - bind a workflow/dataset/app/dashboard/model/report
* ``DELETE /systems/{id}/components/{cid}``  - unbind
* ``DELETE /systems/{id}``                   - dissolve (member objects are untouched)

Every attach is resolved against the live table with owner scoping, so a
system can never reference a foreign or nonexistent object. The health
verdict is derived from the members at read time (nothing stored).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..auth import get_optional_user, own_or_404
from ..db import get_db
from ..models import Py8nSystem, SystemComponent
from ..services.py8n_systems import (
    COMPONENT_KINDS,
    KIND_TABLES,
    resolve_component,
    system_health,
    system_summary,
)

router = APIRouter(prefix="/systems", tags=["systems"])


class SystemCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=140)
    description: str = Field(default="", max_length=2000)
    icon: str = Field(default="boxes", max_length=60)
    color: str = Field(default="#f97316", max_length=20)


class SystemUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=140)
    description: str | None = Field(default=None, max_length=2000)
    icon: str | None = Field(default=None, max_length=60)
    color: str | None = Field(default=None, max_length=20)


class ComponentAttach(BaseModel):
    kind: str = Field(..., description=f"one of: {', '.join(COMPONENT_KINDS)}")
    ref_id: str = Field(..., min_length=1, max_length=36)


async def _get_system(db: AsyncSession, system_id: str, user) -> Py8nSystem:
    # eager-load components: lazy loads on a relationship are a MissingGreenlet
    # trap under asyncio
    row = (
        await db.execute(
            select(Py8nSystem).options(selectinload(Py8nSystem.components)).where(Py8nSystem.id == system_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="System not found")
    own_or_404(row.owner_id, user)
    return row


async def _reload_summary(db: AsyncSession, system_id: str) -> dict:
    """Post-commit summary fetch: re-select with components eagerly loaded."""
    row = (
        await db.execute(
            select(Py8nSystem).options(selectinload(Py8nSystem.components)).where(Py8nSystem.id == system_id)
        )
    ).scalar_one()
    return system_summary(row)


@router.post("", status_code=201)
async def create_system(body: SystemCreate, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = Py8nSystem(name=body.name.strip(), description=body.description.strip(),
                     icon=body.icon, color=body.color)
    row.owner_id = user.id if user else None
    db.add(row)
    await db.commit()
    return await _reload_summary(db, row.id)


@router.get("")
async def list_systems(user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    q = (
        select(Py8nSystem)
        .options(selectinload(Py8nSystem.components))
        .order_by(Py8nSystem.updated_at.desc())
        .limit(100)
    )
    if user:
        q = q.where(Py8nSystem.owner_id.in_([user.id, None]))
    rows = (await db.execute(q)).scalars().unique().all()
    out = []
    for s in rows:
        summary = system_summary(s)
        summary["verdict"] = (await system_health(db, s))["verdict"]
        out.append(summary)
    return out


@router.get("/{system_id}")
async def system_detail(system_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    s = await _get_system(db, system_id, user)
    grouped: dict[str, list] = {k: [] for k in COMPONENT_KINDS}
    for c in s.components or []:
        model = KIND_TABLES.get(c.kind)
        name = None
        if model is not None:
            row = await db.get(model, c.ref_id)
            name = getattr(row, "name", None) if row is not None else None
        grouped[c.kind].append({
            "component_id": c.id,
            "kind": c.kind,
            "ref_id": c.ref_id,
            "name": name or c.ref_id[:8],
            "added_at": c.added_at.isoformat() if c.added_at else None,
        })
    return {
        **system_summary(s),
        "grouped": grouped,
        "health": await system_health(db, s),
    }


@router.put("/{system_id}")
async def update_system(system_id: str, body: SystemUpdate, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    s = await _get_system(db, system_id, user)
    if body.name is not None:
        s.name = body.name.strip()
    if body.description is not None:
        s.description = body.description.strip()
    if body.icon is not None:
        s.icon = body.icon
    if body.color is not None:
        s.color = body.color
    await db.commit()
    return await _reload_summary(db, s.id)


@router.post("/{system_id}/components", status_code=201)
async def attach_component(system_id: str, body: ComponentAttach, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    s = await _get_system(db, system_id, user)
    try:
        await resolve_component(db, body.kind, body.ref_id, user.id if user else None)
    except ValueError as exc:
        raise HTTPException(status_code=404 if "not found" in str(exc) else 400, detail=str(exc)) from exc
    dup = (
        await db.execute(
            select(SystemComponent).where(
                SystemComponent.system_id == s.id,
                SystemComponent.kind == body.kind,
                SystemComponent.ref_id == body.ref_id,
            )
        )
    ).scalar_one_or_none()
    if dup:
        raise HTTPException(status_code=409, detail="that object is already bound to this system")
    comp = SystemComponent(system_id=s.id, kind=body.kind, ref_id=body.ref_id)
    db.add(comp)
    await db.commit()
    await db.refresh(comp)
    fresh = await _reload_summary(db, s.id)
    return {"component_id": comp.id, "kind": comp.kind, "ref_id": comp.ref_id,
            "system_total": fresh["total_components"]}


@router.delete("/{system_id}/components/{component_id}", status_code=204)
async def detach_component(system_id: str, component_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    s = await _get_system(db, system_id, user)
    comp = await db.get(SystemComponent, component_id)
    if comp is None or comp.system_id != s.id:
        raise HTTPException(status_code=404, detail="Component not found")
    await db.delete(comp)
    await db.commit()


@router.delete("/{system_id}", status_code=204)
async def delete_system(system_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    s = await _get_system(db, system_id, user)
    await db.delete(s)  # components cascade; member objects are untouched
    await db.commit()
