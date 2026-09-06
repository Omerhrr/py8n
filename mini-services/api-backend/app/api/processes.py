"""Business processes API (v84) - long-running autonomy.

The business state machine as a first-class primitive: define the machine
(states + named transitions), start instances (the tracked entities that
remember state and context across weeks), advance them (validated against
the machine, loud refusals otherwise, every move on the record + the
business.state_changed event), and measure them (by state, stuck vs SLA,
mean time in state, advance counts - all derived).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..services.business_processes import (
    ProcessError, create_process, get_instance, get_process,
    advance_instance, list_instances, list_processes, process_analytics,
    start_instance,
)
from .auth import get_optional_user

router = APIRouter(prefix="/processes", tags=["processes"])


def _http(exc: ProcessError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


class ProcessCreate(BaseModel):
    name: str
    description: str = ""
    definition: dict = Field(..., description=(
        "{states: [...], initial: str, transitions: [{name, from, to}]}"))


class InstanceStart(BaseModel):
    ref: str = Field(..., description="the external key the business tracks "
                                      "(lead id, case number, phone)")
    title: str = ""
    context: dict = {}
    due_in_seconds: int | None = Field(default=None, ge=1,
                                       description="the SLA promise - stuck-ness derives from it")


class InstanceAdvance(BaseModel):
    transition: str | None = None
    to_state: str | None = None
    actor: str = ""
    note: str = ""
    context_patch: dict = {}
    payload: dict = {}
    due_in_seconds: int | None = Field(default=None, ge=1)


@router.post("", status_code=201)
async def create(body: ProcessCreate, user=Depends(get_optional_user),
                 db: AsyncSession = Depends(get_db)):
    try:
        out = await create_process(db, owner_id=getattr(user, "id", None),
                                   name=body.name, description=body.description,
                                   definition=body.definition)
    except ProcessError as exc:
        raise _http(exc) from exc
    await db.commit()
    return out


@router.get("")
async def list_all(user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    return {"processes": await list_processes(db, getattr(user, "id", None))}


@router.get("/{process_id}")
async def detail(process_id: str, user=Depends(get_optional_user),
                 db: AsyncSession = Depends(get_db)):
    try:
        return await get_process(db, process_id, getattr(user, "id", None))
    except ProcessError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{process_id}/analytics")
async def analytics(process_id: str, user=Depends(get_optional_user),
                    db: AsyncSession = Depends(get_db)):
    try:
        return await process_analytics(db, process_id, getattr(user, "id", None))
    except ProcessError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{process_id}/instances", status_code=201)
async def start(process_id: str, body: InstanceStart, user=Depends(get_optional_user),
                db: AsyncSession = Depends(get_db)):
    try:
        out = await start_instance(db, process_id,
                                   owner_id=getattr(user, "id", None),
                                   ref=body.ref, title=body.title,
                                   context=body.context,
                                   due_in_seconds=body.due_in_seconds,
                                   actor=getattr(user, "id", None) or "api")
    except ProcessError as exc:
        raise _http(exc) from exc
    await db.commit()
    return out


@router.get("/{process_id}/instances")
async def instances(process_id: str, state: str | None = None,
                    user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    try:
        return {"instances": await list_instances(
            db, process_id, getattr(user, "id", None), state=state)}
    except ProcessError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{process_id}/instances/{instance_id}")
async def instance(process_id: str, instance_id: str,
                   user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    try:
        return await get_instance(db, process_id, instance_id, getattr(user, "id", None))
    except ProcessError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{process_id}/instances/{instance_id}/advance")
async def advance(process_id: str, instance_id: str, body: InstanceAdvance,
                  user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    try:
        out = await advance_instance(
            db, instance_id, owner_id=getattr(user, "id", None),
            transition=body.transition, to_state=body.to_state,
            actor=body.actor or (getattr(user, "id", None) or "api"),
            note=body.note, context_patch=body.context_patch,
            payload=body.payload, due_in_seconds=body.due_in_seconds)
    except ProcessError as exc:
        raise _http(exc) from exc
    await db.commit()
    return out
