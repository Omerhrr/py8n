"""Business processes: _aware, instance_out, _log_out, _load_instance, start_instance, advance_instance, instance_journey, _resolve_process_ref, _render_journey_template, fire_journeys, get_instance, list_instances, query_instances.

Split from app/services/business_processes.py (task #3) - moved verbatim,
no behavior change.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...models import (BusinessProcess, BusinessProcessInstance,
                      BusinessProcessTransitionLog, ChainReportSchedule)
from .. import escalations as escalations_svc  # v87: the channel + repeat policy layer
from ._shared import (ACK_TRANSITION, ANNOTATE_TRANSITION, CHAIN_CSV_HEADER,
                      CHAIN_NAMES, CHAIN_REPORT_CADENCES, CHAIN_REPORT_MAX_CHAINS,
                      CHAIN_REPORT_MAX_RECIPIENTS, CHAIN_REPORT_MIN_CADENCE,
                      DIGEST_TRANSITION, ESCALATION_HISTORY_TRANSITIONS,
                      ESCALATION_TRANSITION, PAPERWORK_TRANSITIONS,
                      ProcessError, RESERVED_CONTEXT_KEYS, SIDE_LEGS_CHAIN, _now)
from .definitions import _allowed_from, _terminal_states
from .process_crud import _load_process

def _aware(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes - normalize before arithmetic."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def instance_out(row: BusinessProcessInstance, *, definition: dict | None = None,
                 journey: list[dict] | None = None) -> dict:
    now = _now()
    entered = _aware(row.entered_state_at)
    age = (now - entered).total_seconds() if entered else 0
    terminal = sorted(_terminal_states(definition)) if definition else []
    out = {
        "id": row.id, "process_id": row.process_id, "ref": row.ref,
        "title": row.title, "state": row.state, "context": row.context or {},
        "entered_state_at": row.entered_state_at.isoformat() if row.entered_state_at else None,
        "due_at": row.due_at.isoformat() if row.due_at else None,
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "age_in_state_seconds": round(age),
        "is_terminal": bool(terminal) and row.state in terminal,
        "is_stuck": bool(row.due_at and row.ended_at is None and now > _aware(row.due_at)),
    }
    if journey is not None:
        out["journey"] = journey
    return out


def _log_out(t: BusinessProcessTransitionLog) -> dict:
    return {"id": t.id, "from_state": t.from_state, "to_state": t.to_state,
            "transition": t.transition, "actor": t.actor, "note": t.note,
            "payload": t.payload or {},
            "at": t.created_at.isoformat() if t.created_at else None}


async def _load_instance(db: AsyncSession, instance_id: str, owner_id: str | None) -> BusinessProcessInstance:
    row = await db.get(BusinessProcessInstance, instance_id)
    if row is None or (owner_id is not None and row.owner_id not in (owner_id, None)):
        raise ProcessError(f"instance {instance_id!r} not found")
    return row


async def start_instance(db: AsyncSession, process_id: str, *, owner_id: str | None,
                         ref: str, title: str = "", context: dict | None = None,
                         due_in_seconds: int | None = None,
                         actor: str = "",
                         state: str | None = None) -> dict:
    """Track a new entity. ``state`` optionally begins the journey at a
    named state instead of the initial one - how businesses IMPORT the
    entities they already track (a CRM lead imported as 'contacted' has
    not travelled the machine's road; the started row says so honestly).
    Everything else is validated like any other move."""
    p = await _load_process(db, process_id, owner_id)
    ref = str(ref or "").strip()
    if not ref:
        raise ProcessError("an instance needs a ref - the external key the "
                           "business already tracks (lead id, case number, phone)")
    definition = p.definition or {}
    begin = (definition.get("initial") or "")
    if state is not None:
        begin = str(state).strip()
        if begin not in (definition.get("states") or []):
            raise ProcessError(f"cannot start at {begin!r} - not a state of this "
                               f"machine (states: {definition.get('states')})")
    now = _now()
    row = BusinessProcessInstance(
        process_id=p.id, ref=ref[:180], title=(title or "").strip()[:200],
        state=begin,
        context=dict(context or {}),
        due_at=(now + timedelta(seconds=max(1, int(due_in_seconds))))
               if due_in_seconds else None)
    row.owner_id = owner_id
    row.entered_state_at = now
    db.add(row)
    await db.flush()
    await db.refresh(row)
    db.add(BusinessProcessTransitionLog(
        process_id=p.id, instance_id=row.id, from_state=None,
        to_state=row.state, transition="started", actor=(actor or "system")[:140],
        note="instance started (imported mid-machine)" if state is not None
             else "instance started",
        payload={"ref": row.ref}))
    return instance_out(row, definition=p.definition)


async def advance_instance(db: AsyncSession, instance_id: str, *, owner_id: str | None,
                           transition: str | None = None, to_state: str | None = None,
                           actor: str = "", note: str = "",
                           context_patch: dict | None = None,
                           payload: dict | None = None,
                           due_in_seconds: int | None = None,
                           process_id: str | None = None) -> dict:
    """Move the instance. Resolve by transition name OR direct to_state;
    anything the machine does not allow is a loud refusal naming the
    allowed moves. The context patch MERGES into the running memory (fresh
    dicts - the JSON column is never mutated in place).

    v105: when the caller names the process on the path (the API always
    does), the instance must belong to it - the same cross-check every
    other instance door keeps, because v105's system-scoped authority is
    decided on the NAMED machine and must never move another machine's
    entity through a mismatched path."""
    row = await _load_instance(db, instance_id, owner_id)
    if process_id is not None and row.process_id != process_id:
        raise ProcessError(
            f"instance {instance_id!r} is not in process {process_id!r}")
    p = await _load_process(db, row.process_id, owner_id)
    definition = p.definition or {}
    current = row.state
    allowed = _allowed_from(definition, current)
    if not allowed:
        raise ProcessError(f"state {current!r} is terminal - the machine has "
                           "no outgoing moves; the journey is complete")
    chosen: dict | None = None
    if transition:
        t = str(transition).strip()
        chosen = next((x for x in allowed if x["name"] == t), None)
        if chosen is None:
            raise ProcessError(f"no transition {t!r} from state {current!r} - "
                               f"allowed: {[x['name'] for x in allowed]}")
    elif to_state:
        t = str(to_state).strip()
        chosen = next((x for x in allowed if x["to"] == t), None)
        if chosen is None:
            raise ProcessError(f"no move from {current!r} to {t!r} - "
                               f"allowed: {[(x['name'], x['to']) for x in allowed]}")
    else:
        raise ProcessError("name the move: transition (by name) or to_state")

    now = _now()
    new_ctx = dict(row.context or {})
    if context_patch:
        if not isinstance(context_patch, dict):
            raise ProcessError("context_patch must be an object")
        new_ctx.update(context_patch)
    row.context = new_ctx
    row.state = chosen["to"]
    row.entered_state_at = now
    if due_in_seconds is not None:
        row.due_at = now + timedelta(seconds=max(1, int(due_in_seconds)))
    terminal = _terminal_states(definition)
    row.ended_at = now if chosen["to"] in terminal else None
    db.add(row)
    log = BusinessProcessTransitionLog(
        process_id=p.id, instance_id=row.id, from_state=current,
        to_state=chosen["to"], transition=chosen["name"],
        actor=(actor or "system")[:140], note=(note or "")[:500],
        payload=dict(payload or {}))
    db.add(log)
    await db.flush()
    await db.refresh(row)

    # the business moved - the event system says so (workflows react)
    from .. import system_events as events_svc

    await events_svc.emit(
        db, row.owner_id, "business.state_changed", source="business",
        actor=(actor or "system"), target_type="process_instance",
        target_id=row.id,
        payload={"process_id": p.id, "process_name": p.name,
                 "instance_id": row.id, "ref": row.ref, "title": row.title,
                 "from": current, "to": chosen["to"],
                 "transition": chosen["name"], "note": (note or "")[:200]},
        correlation_id=row.id)
    journey = await instance_journey(db, row.id)
    out = instance_out(row, definition=definition, journey=journey)
    # v89: the machine landed on a fire-state - the next leg opens itself
    opened = await fire_journeys(db, row, p, definition, to_state=chosen["to"],
                                 actor=(actor or "system"), now=now)
    if opened:
        out["journeys_opened"] = opened
    return out


async def instance_journey(db: AsyncSession, instance_id: str) -> list[dict]:
    rows = (await db.execute(
        select(BusinessProcessTransitionLog)
        .where(BusinessProcessTransitionLog.instance_id == instance_id)
        .order_by(BusinessProcessTransitionLog.created_at.asc()))).scalars().all()
    return [_log_out(t) for t in rows]


async def _resolve_process_ref(db: AsyncSession, want: str,
                               owner_id: str | None) -> BusinessProcess | None:
    """Find a process by id or case-insensitive name (most recent wins),
    owner-scoped - the same resolution the read door uses, because the
    journey's target is named at definition time and resolved when the
    machine actually lands (operators install independently)."""
    want = str(want or "").strip()
    if not want:
        return None
    prow = (await db.execute(
        select(BusinessProcess).where(BusinessProcess.id == want))).scalars().first()
    if prow is None:
        prow = (await db.execute(
            select(BusinessProcess)
            .where(func.lower(BusinessProcess.name) == want.lower())
            .order_by(BusinessProcess.created_at.desc()))).scalars().first()
    if prow is None:
        return None
    if owner_id is not None and prow.owner_id not in (owner_id, None):
        return None
    return prow


def _render_journey_template(template: str, fields: dict) -> str:
    """Fill a journey's title/ref template from the source instance's
    fields ({ref} {title} {state} {process}) - unknown placeholders stay
    literal, a broken or empty template renders empty (the caller falls
    back to the source's own values)."""
    if not template:
        return ""
    try:
        return str(template.format(**fields)).strip()
    except Exception:  # noqa: BLE001 - a broken template must not stop the deal
        return ""


async def fire_journeys(db: AsyncSession, row: BusinessProcessInstance,
                        process: BusinessProcess, definition: dict, *,
                        to_state: str, actor: str,
                        now: datetime) -> list[dict]:
    """The machine LANDED on a fire-state - open the next leg.

    Every journey keyed on to_state opens an instance on its target
    machine (resolved by name/id, owner-scoped): the ref/title render
    from the source's fields, the opened context carries the spec's
    memory plus the journey link (from_process / from_instance /
    from_ref / from_state), and the leg is on the record BOTH ways -
    business.journey_opened on the source's correlation thread, and an
    honest business.journey_skipped when the target machine does not
    exist yet (the target operator was never installed), already tracks
    an open instance with that ref (never double-tracks), or refuses the
    start (a bad state= naming). Journeys fire on ADVANCES only - the
    opened leg starts its own life and its own future fires; nothing
    recurses through the start itself."""
    journeys = [j for j in (definition.get("journeys") or [])
                if j.get("on_state") == to_state]
    if not journeys:
        return []
    from .. import system_events as events_svc

    out: list[dict] = []
    for j in journeys:
        spec = j["open"]
        entry: dict = {"on_state": to_state, "target_process": spec["process"]}
        fields = {"ref": row.ref, "title": row.title, "state": to_state,
                  "process": process.name}
        ref = _render_journey_template(spec.get("ref_template"), fields) or row.ref
        title = _render_journey_template(spec.get("title_template"), fields) or row.title
        entry["ref"] = ref
        target = await _resolve_process_ref(db, spec["process"], row.owner_id)
        if target is None:
            entry["opened"] = False
            entry["reason"] = (f"target process {spec['process']!r} not found - "
                               "install the operator that ships it and the leg "
                               "opens on the next landing")
            await events_svc.emit(
                db, row.owner_id, "business.journey_skipped", source="business",
                actor=actor or "journey", target_type="process_instance",
                target_id=row.id,
                payload={"process_id": process.id, "process_name": process.name,
                         "instance_id": row.id, "ref": row.ref,
                         "title": row.title, "on_state": to_state,
                         "target_process": spec["process"], "reason": entry["reason"]},
                correlation_id=row.id)
            out.append(entry)
            continue
        dup = (await db.execute(
            select(BusinessProcessInstance)
            .where(BusinessProcessInstance.process_id == target.id,
                   BusinessProcessInstance.ref == ref,
                   BusinessProcessInstance.ended_at.is_(None)))).scalars().first()
        if dup is not None:
            entry["opened"] = False
            entry["reason"] = (f"an open instance of {target.name!r} already "
                               f"carries ref {ref!r} - the journey never "
                               "double-tracks")
            await events_svc.emit(
                db, row.owner_id, "business.journey_skipped", source="business",
                actor=actor or "journey", target_type="process_instance",
                target_id=row.id,
                payload={"process_id": process.id, "process_name": process.name,
                         "instance_id": row.id, "ref": row.ref,
                         "on_state": to_state, "target_process": target.name,
                         "target_ref": ref, "reason": entry["reason"]},
                correlation_id=row.id)
            out.append(entry)
            continue
        context = dict(spec.get("memory") or {})
        context["journey"] = {"from_process": process.name,
                              "from_instance": row.id,
                              "from_ref": row.ref,
                              "from_state": to_state,
                              "at": now.isoformat()}
        try:
            started = await start_instance(
                db, target.id, owner_id=row.owner_id, ref=ref,
                title=title[:200], context=context,
                state=spec.get("state") or None,
                due_in_seconds=spec.get("due_in_seconds"),
                actor=actor or "journey")
        except ProcessError as exc:
            entry["opened"] = False
            entry["reason"] = str(exc)
            await events_svc.emit(
                db, row.owner_id, "business.journey_skipped", source="business",
                actor=actor or "journey", target_type="process_instance",
                target_id=row.id,
                payload={"process_id": process.id, "process_name": process.name,
                         "instance_id": row.id, "ref": row.ref,
                         "on_state": to_state, "target_process": target.name,
                         "reason": entry["reason"][:300]},
                correlation_id=row.id)
            out.append(entry)
            continue
        entry["opened"] = True
        entry["target"] = {"process_id": target.id, "process_name": target.name,
                           "instance_id": started["id"], "ref": started["ref"],
                           "title": started["title"], "state": started["state"],
                           "due_at": started.get("due_at")}
        await events_svc.emit(
            db, row.owner_id, "business.journey_opened", source="business",
            actor=actor or "journey", target_type="process_instance",
            target_id=row.id,
            payload={"process_id": process.id, "process_name": process.name,
                     "instance_id": row.id, "ref": row.ref, "title": row.title,
                     "on_state": to_state, "target": entry["target"]},
            correlation_id=row.id)
        out.append(entry)
    return out


async def get_instance(db: AsyncSession, process_id: str, instance_id: str,
                       owner_id: str | None) -> dict:
    row = await _load_instance(db, instance_id, owner_id)
    if row.process_id != process_id:
        raise ProcessError(f"instance {instance_id!r} is not in process {process_id!r}")
    p = await _load_process(db, process_id, owner_id)
    journey = await instance_journey(db, row.id)
    return instance_out(row, definition=p.definition, journey=journey)


async def list_instances(db: AsyncSession, process_id: str, owner_id: str | None,
                         state: str | None = None) -> list[dict]:
    await _load_process(db, process_id, owner_id)
    q = (select(BusinessProcessInstance)
         .where(BusinessProcessInstance.process_id == process_id)
         .order_by(BusinessProcessInstance.entered_state_at.desc()))
    if state:
        q = q.where(BusinessProcessInstance.state == state)
    rows = (await db.execute(q)).scalars().all()
    p_def = (await _load_process(db, process_id, owner_id)).definition or {}
    return [instance_out(r, definition=p_def) for r in rows]


async def query_instances(db: AsyncSession, *, owner_id: str | None,
                          process: str | None = None, state: str | None = None,
                          ref: str | None = None, stuck_only: bool = False,
                          open_only: bool = True, limit: int = 50) -> dict:
    """Read the RUNNING entities across processes (v87) - the agents' door
    onto the operation. Resolve the process by id or case-insensitive name
    (most recent wins), filter by state / ref / stuck-ness, derive the
    per-instance view. Owner-scoped like every read: a NAMED query on
    another owner's process refuses loud (404-grade hiding), an unnamed
    one simply reads an empty operation."""
    limit = max(1, min(int(limit or 50), 500))
    process_name: str | None = None
    definitions: dict[str, dict] = {}
    names: dict[str, str] = {}

    q = select(BusinessProcessInstance)
    if process and str(process).strip():
        want = str(process).strip()
        prow = (await db.execute(
            select(BusinessProcess).where(BusinessProcess.id == want))).scalars().first()
        if prow is None:
            prow = (await db.execute(
                select(BusinessProcess)
                .where(func.lower(BusinessProcess.name) == want.lower())
                .order_by(BusinessProcess.created_at.desc()))).scalars().first()
        if prow is None or (owner_id is not None and prow.owner_id not in (owner_id, None)):
            raise ProcessError(f"process {process!r} not found")
        q = q.where(BusinessProcessInstance.process_id == prow.id)
        process_name = prow.name
        definitions[prow.id] = prow.definition or {}
        names[prow.id] = prow.name
    else:
        procs = (await db.execute(select(BusinessProcess))).scalars().all()
        for prow in procs:
            if owner_id is not None and prow.owner_id not in (owner_id, None):
                continue
            definitions[prow.id] = prow.definition or {}
            names[prow.id] = prow.name

    if owner_id is not None:
        q = q.where(BusinessProcessInstance.owner_id.in_((owner_id, None)))
    if state and str(state).strip():
        q = q.where(BusinessProcessInstance.state == str(state).strip())
    if ref and str(ref).strip():
        q = q.where(BusinessProcessInstance.ref == str(ref).strip())
    q = (q.order_by(BusinessProcessInstance.entered_state_at.desc())
          .limit(limit))
    rows = (await db.execute(q)).scalars().all()

    out = []
    for r in rows:
        if r.process_id not in definitions:
            continue  # another owner's process - never leak it
        o = instance_out(r, definition=definitions[r.process_id])
        o["process_name"] = names.get(r.process_id, "")
        if open_only and o["is_terminal"]:
            continue
        if stuck_only and not o["is_stuck"]:
            continue
        out.append(o)
    return {"instances": out, "count": len(out), "process": process_name,
            "filters": {"state": (state or "").strip(), "ref": (ref or "").strip(),
                        "stuck_only": bool(stuck_only), "open_only": bool(open_only),
                        "limit": limit}}

