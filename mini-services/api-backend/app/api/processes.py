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
    ProcessError, acknowledge_escalation, advance_instance, annotate_instance,
    attention_feed, create_process, get_instance, get_process, list_instances,
    list_processes, process_analytics, start_instance, update_escalation_policy,
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


@router.get("/attention")
async def attention(limit: int = 200, user=Depends(get_optional_user),
                    db: AsyncSession = Depends(get_db)):
    """v91: the overdue-attention view - every OPEN instance past its SLA
    across ALL machines, most-overdue first, with the door's escalation
    book per row. Declared BEFORE /{process_id} so the word 'attention'
    is never eaten as a process id."""
    return await attention_feed(db, getattr(user, "id", None), limit=limit)


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


class InstanceAnnotate(BaseModel):
    """v88: facts land on the entity's memory - the machine does not move."""

    context_patch: dict = Field(..., description=(
        "the facts to remember, {key: value} - merged into the instance's "
        "running memory; the reserved 'escalations' key refuses loud"))
    actor: str = Field(default="", description="who learned this (agent name, staff id)")
    note: str = Field(default="", description="why these facts landed (journey log)")


@router.post("/{process_id}/instances/{instance_id}/annotate")
async def annotate(process_id: str, instance_id: str, body: InstanceAnnotate,
                   user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """v88: the agents' memory door - write facts DIRECTLY into the
    entity's running context without moving the machine. On the record
    (an 'annotate' journey row naming the keys) + business.annotated on
    the correlation thread, so a workflow can react to a FACT landing."""
    try:
        out = await annotate_instance(
            db, process_id, instance_id, owner_id=getattr(user, "id", None),
            context_patch=body.context_patch,
            actor=body.actor or (getattr(user, "id", None) or "api"),
            note=body.note)
    except ProcessError as exc:
        raise _http(exc) from exc
    await db.commit()
    return out


class PolicyUpdate(BaseModel):
    """v91: the escalation policy, edited from the board - the same loud
    validation the definition carries (unknown keys / channels, the two
    clocks, the to-vs-handlers mutex all refuse); null removes the policy
    (the machine falls back to one knock per stint, event-only)."""

    policy: dict | None = Field(default=None, description=(
        "the new escalation_policy (channel, to|handlers, mode, cadence, "
        "max_repeats, message_template) - null removes it"))
    actor: str = Field(default="", description="who changed the rhythm")


@router.patch("/{process_id}/escalation-policy")
async def edit_policy(process_id: str, body: PolicyUpdate,
                      user=Depends(get_optional_user),
                      db: AsyncSession = Depends(get_db)):
    """v91: edit (or remove) the machine's escalation policy - the door
    reads the policy fresh at every sweep, so the new rhythm takes effect
    on the next tick without touching the running instances."""
    try:
        out = await update_escalation_policy(
            db, process_id, owner_id=getattr(user, "id", None),
            policy=body.policy, actor=body.actor)
    except ProcessError as exc:
        raise _http(exc) from exc
    await db.commit()
    return out


class EscalationAck(BaseModel):
    """v88: the human's receipt - a named take of the escalation.
    v89: snooze_hours turns the take into a loan - the door re-knocks
    once the snooze runs out (omit it to own the rest of the stint)."""

    by: str = Field(..., min_length=1, description="who acknowledged (the receipt's name)")
    note: str = Field(default="", description="the handler's own words")
    snooze_hours: float | None = Field(default=None, ge=0, description=(
        "hold the door quiet for N hours, then it re-knocks on its cadence "
        "(re-acking replaces the loan; omitted = the take owns the rest of "
        "the state stint)"))


@router.post("/{process_id}/instances/{instance_id}/escalations/ack")
async def ack_escalation(process_id: str, instance_id: str, body: EscalationAck,
                         user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """v88: acknowledge the instance's escalation episode - the door goes
    quiet (the handler who said 'I have this' owns it); the receipt is on
    the record and business.escalation_acknowledged lands on the
    correlation thread. v89: snooze_hours re-arms the door after N hours."""
    try:
        out = await acknowledge_escalation(
            db, process_id, instance_id, owner_id=getattr(user, "id", None),
            by=body.by, note=body.note, snooze_hours=body.snooze_hours)
    except ProcessError as exc:
        raise _http(exc) from exc
    await db.commit()
    return out
