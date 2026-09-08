"""Business processes API (v84) - long-running autonomy.

The business state machine as a first-class primitive: define the machine
(states + named transitions), start instances (the tracked entities that
remember state and context across weeks), advance them (validated against
the machine, loud refusals otherwise, every move on the record + the
business.state_changed event), and measure them (by state, stuck vs SLA,
mean time in state, advance counts - all derived).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..services.business_processes import (
    ProcessError, acknowledge_escalation, advance_instance, annotate_instance,
    attention_feed, chain_history_csv, chain_map, create_process,
    delete_chain_report, escalation_day_detail, escalation_history_grid,
    escalation_preview, get_chain_report, get_instance, get_process,
    list_instances, list_processes, process_analytics, send_chain_report_now,
    start_instance, update_escalation_policy, upsert_chain_report,
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


@router.get("/chains")
async def chains(history_limit: int = 5, user=Depends(get_optional_user),
                 db: AsyncSession = Depends(get_db)):
    """v95: the owner-wide chain map - the cross-machine journey chains
    derived from what is INSTALLED, each node with its live counts, each
    leg with its ride counts and the recent traversals (the HISTORY the
    operator-detail chain overlays). Declared BEFORE /{process_id}.
    v96: history_limit stretches the per-leg traversal window beyond the
    default 5 (clamped 1..50) - deeper chain history for legs that have
    ridden for months."""
    return await chain_map(db, getattr(user, "id", None),
                           history_limit=history_limit)


@router.get("/chains/history.csv")
async def chains_history_csv_route(history_limit: int = 50,
                                   chain: str = "",
                                   leg: str = "",
                                   user=Depends(get_optional_user),
                                   db: AsyncSession = Depends(get_db)):
    """v98: the per-leg chain history as a CSV download - the SAME map the
    operator-detail chain draws (chain_map, zero drift), one row per
    traversal with the chain and the leg named on every row (a leg with no
    rides yet still ships one row, the absence reading as data).
    history_limit rides the map's own clamp (1..50); the default is the
    deepest window. Declared BEFORE /{process_id} (alongside /chains).

    v99: the PLOT's own filters - ``chain`` names one chain (the per-chain
    CSV button), ``leg`` names one leg as "from_name|on_state" (the
    per-leg CSV chip); both compose, case-insensitive, an unknown name an
    honest empty file. The response names the filters it honored."""
    out = await chain_history_csv(db, getattr(user, "id", None),
                                  history_limit=history_limit,
                                  chain=chain or None, leg=leg or None)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    slug = ""
    if out["chain_filter"]:
        slug = "-" + "".join(c if c.isalnum() else "-"
                             for c in out["chain_filter"].lower())[:40].strip("-")
    headers = {"Content-Disposition":
               f'attachment; filename="py8n-chain-history{slug}-{stamp}.csv"',
               "X-Py8n-Leg-Count": str(out["leg_count"]),
               "X-Py8n-Ride-Count": str(out["ride_count"])}
    if out["chain_filter"]:
        headers["X-Py8n-Chain-Filter"] = out["chain_filter"]
    if out["leg_filter"]:
        headers["X-Py8n-Leg-Filter"] = out["leg_filter"]
    return Response(content=out["csv"], media_type="text/csv; charset=utf-8",
                    headers=headers)


class ChainReportUpsert(BaseModel):
    """v99: the chain-report schedule - the digest pattern applied to the
    FILE. One per owner: cadence floor 300s (a file dispatch is a minutes
    concern), a real recipient, the depth clamped to the map's 1..50, one
    optional chain name ("" = the whole estate map)."""
    enabled: bool = True
    cadence_seconds: int = Field(default=86400, description=(
        "how often the file rides the wire (floor 300s)"))
    to: str = Field(default="", description="the recipient (email address)")
    history_limit: int = Field(default=50, ge=1, le=50, description=(
        "the per-leg traversal window (the map's own clamp)"))
    chain: str = Field(default="", description=(
        "optional single-chain filter (\"\" = every chain)"))


@router.get("/chain-report")
async def get_chain_report_route(user=Depends(get_optional_user),
                                 db: AsyncSession = Depends(get_db)):
    """v99: the owner's chain-report schedule, or the honest absence."""
    return await get_chain_report(db, getattr(user, "id", None))


@router.put("/chain-report")
async def put_chain_report_route(body: ChainReportUpsert,
                                 user=Depends(get_optional_user),
                                 db: AsyncSession = Depends(get_db)):
    """v99: create or update the schedule - validated loud, next_due
    re-anchored to now + cadence (the new cadence rules the NEXT window,
    the digest's own re-anchor)."""
    try:
        out = await upsert_chain_report(
            db, getattr(user, "id", None), enabled=body.enabled,
            cadence_seconds=body.cadence_seconds, to=body.to,
            history_limit=body.history_limit, chain=body.chain)
    except ProcessError as exc:
        raise _http(exc) from exc
    await db.commit()
    return out


@router.delete("/chain-report")
async def delete_chain_report_route(user=Depends(get_optional_user),
                                    db: AsyncSession = Depends(get_db)):
    """v99: remove the schedule - removing is not pausing, the file stops
    riding the door entirely."""
    try:
        out = await delete_chain_report(db, getattr(user, "id", None))
    except ProcessError as exc:
        raise _http(exc) from exc
    await db.commit()
    return out


@router.post("/chain-report/send-now")
async def send_chain_report_now_route(user=Depends(get_optional_user),
                                      db: AsyncSession = Depends(get_db)):
    """v99: the manual door - one dispatch NOW, whatever next_due says
    (a real send is a real send: last_sent_at/next_due/last_result all
    stamp). The same renderer the scheduled walk uses, zero drift."""
    try:
        out = await send_chain_report_now(db, getattr(user, "id", None))
    except ProcessError as exc:
        raise _http(exc) from exc
    await db.commit()
    return out


@router.get("/escalation-history")
async def escalation_history_grid_route(days: int = 14,
                                        user=Depends(get_optional_user),
                                        db: AsyncSession = Depends(get_db)):
    """v95: the CROSS-MACHINE escalation history - per machine, per day,
    the door's activity (escalations / digests) and the humans' (acks),
    read straight off the transition log. The heatmap's grid (v94's
    per-machine sparkline lives in the analytics; this is the estate
    wide view beside it). Declared BEFORE /{process_id}."""
    return await escalation_history_grid(db, getattr(user, "id", None), days=days)


@router.get("/escalation-history/{process_id}/{day}")
async def escalation_day(process_id: str, day: str,
                         user=Depends(get_optional_user),
                         db: AsyncSession = Depends(get_db)):
    """v96: ONE heatmap cell, opened - the machine's day. Every escalation
    row the door wrote on that machine between the day's midnight and the
    next, with the entity each belongs to: the drill-down the /processes
    heatmap cell click serves. Declared BEFORE /{process_id} so the
    literal 'escalation-history' prefix is never eaten as an id; a badly
    shaped day refuses loud (400) before the process is even loaded
    (unknown machines hide 404, the same as every other read)."""
    day_s = str(day or "").strip()
    try:
        datetime.fromisoformat(f"{day_s}T00:00:00+00:00")
    except ValueError:
        raise HTTPException(status_code=400, detail=(
            f"day {day_s!r} is not an ISO date (YYYY-MM-DD) - the heatmap "
            "cells name their day")) from None
    try:
        return await escalation_day_detail(db, process_id, day_s,
                                           getattr(user, "id", None))
    except ProcessError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


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


class PolicyPreview(BaseModel):
    """v93: the DRAFT policy the board's editor is holding - the preview
    renders the door's next move under these rules without saving them
    (the same validator a save runs, so a broken draft refuses here
    first)."""

    policy: dict | None = Field(default=None, description=(
        "the draft escalation_policy to preview (channel, to|handlers, mode, "
        "cadence, max_repeats, message_template) - null previews the v85 "
        "no-policy semantics"))


@router.post("/{process_id}/escalation-preview")
async def preview_policy(process_id: str, body: PolicyPreview,
                         user=Depends(get_optional_user),
                         db: AsyncSession = Depends(get_db)):
    """v93: the SLA digest preview - what the door would do RIGHT NOW
    under the draft policy, rendered over the machine's LIVE overdue
    items: the digest's subject + body (or the per-item knock messages),
    the held episodes (acked / snoozed / capped) and the honest delivery
    note (event-only / no target / no endpoint bound). Nothing is saved,
    sent or recorded - the door's next move, typeset for the editor."""
    try:
        return await escalation_preview(
            db, process_id, owner_id=getattr(user, "id", None),
            policy=body.policy)
    except ProcessError as exc:
        raise _http(exc) from exc


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
    once the snooze runs out (omit it to own the rest of the stint).
    v96: reschedule_in_minutes names the door's next knock EXPLICITLY -
    a reschedule_at stamp on the receipt; one clock per receipt (both
    together refuse loud)."""

    by: str = Field(..., min_length=1, description="who acknowledged (the receipt's name)")
    note: str = Field(default="", description="the handler's own words")
    snooze_hours: float | None = Field(default=None, ge=0, description=(
        "hold the door quiet for N hours, then it re-knocks on its cadence "
        "(re-acking replaces the loan; omitted = the take owns the rest of "
        "the state stint)"))
    reschedule_in_minutes: float | None = Field(default=None, ge=0, description=(
        "v96: re-knock the door at an EXPLICIT moment - now + N minutes "
        "(the human picked the time, not a duration; mutually exclusive "
        "with snooze_hours)"))


@router.post("/{process_id}/instances/{instance_id}/escalations/ack")
async def ack_escalation(process_id: str, instance_id: str, body: EscalationAck,
                         user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """v88: acknowledge the instance's escalation episode - the door goes
    quiet (the handler who said 'I have this' owns it); the receipt is on
    the record and business.escalation_acknowledged lands on the
    correlation thread. v89: snooze_hours re-arms the door after N hours.
    v96: reschedule_in_minutes re-arms it at an explicit moment."""
    try:
        out = await acknowledge_escalation(
            db, process_id, instance_id, owner_id=getattr(user, "id", None),
            by=body.by, note=body.note, snooze_hours=body.snooze_hours,
            reschedule_in_minutes=body.reschedule_in_minutes)
    except ProcessError as exc:
        raise _http(exc) from exc
    await db.commit()
    return out
