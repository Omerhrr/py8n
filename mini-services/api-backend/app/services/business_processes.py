"""Business processes (v84) - long-running autonomy: the business state
machine.

Workflows are MOMENTS (trigger in, run, done). Businesses run on things
that stay open for days or months: a lead moving lead -> contacted ->
interested -> demo -> proposal -> negotiating -> won, a support case, an
insurance claim, an appointment, a delivery, a loan application. A
BusinessProcess is the MACHINE (states + named transitions); instances
are the tracked entities that REMEMBER state and context across weeks;
every advance is on the record (the transition log) and emits
business.state_changed through the v80 event door - so workflows and
agents react to the business moving ("proposal_sent -> send the
follow-up"; "stuck > SLA -> escalate"). Business state machine + agents +
workflows + data + interactions = long-running autonomy.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (BusinessProcess, BusinessProcessInstance,
                      BusinessProcessTransitionLog)


class ProcessError(ValueError):
    """Honest business-process failures."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# the definition - a validated state machine
# ---------------------------------------------------------------------------

def validate_definition(definition: dict | None) -> dict:
    """Validate the machine: states exist, transitions reference them, one
    initial state, no duplicate (name, from) pairs. Raises ProcessError."""
    d = definition if isinstance(definition, dict) else {}
    states = d.get("states")
    if not isinstance(states, list) or not states:
        raise ProcessError("definition.states must be a non-empty list of state names")
    clean_states: list[str] = []
    for s in states:
        s = str(s).strip()
        if not s:
            raise ProcessError("state names cannot be empty")
        if s in clean_states:
            raise ProcessError(f"duplicate state {s!r}")
        clean_states.append(s)
    initial = str(d.get("initial") or "").strip()
    if not initial:
        raise ProcessError("definition.initial is required")
    if initial not in clean_states:
        raise ProcessError(f"initial state {initial!r} is not in states {clean_states}")
    raw_transitions = d.get("transitions")
    if not isinstance(raw_transitions, list) or not raw_transitions:
        raise ProcessError("definition.transitions must be a non-empty list "
                           "- a machine with no moves is not a machine")
    clean_transitions: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for t in raw_transitions:
        t = t if isinstance(t, dict) else {}
        name = str(t.get("name") or "").strip()
        frm = str(t.get("from") or "").strip()
        to = str(t.get("to") or "").strip()
        if not name:
            raise ProcessError("every transition needs a name")
        if frm not in clean_states:
            raise ProcessError(f"transition {name!r}: 'from' state {frm!r} is not in states")
        if to not in clean_states:
            raise ProcessError(f"transition {name!r}: 'to' state {to!r} is not in states")
        if (name, frm) in seen:
            raise ProcessError(f"duplicate transition {name!r} from {frm!r}")
        seen.add((name, frm))
        clean_transitions.append({"name": name, "from": frm, "to": to,
                                  "description": str(t.get("description") or "")})
    return {"states": clean_states, "initial": initial,
            "transitions": clean_transitions}


def _terminal_states(definition: dict) -> set[str]:
    outgoing = {t["from"] for t in definition["transitions"]}
    return {s for s in definition["states"] if s not in outgoing}


def _allowed_from(definition: dict, state: str) -> list[dict]:
    return [t for t in definition["transitions"] if t["from"] == state]


# ---------------------------------------------------------------------------
# process CRUD (definitions)
# ---------------------------------------------------------------------------

def process_out(p: BusinessProcess, *, instance_counts: dict | None = None) -> dict:
    d = p.definition or {}
    return {"id": p.id, "name": p.name, "description": p.description,
            "definition": d, "states": d.get("states", []),
            "initial": d.get("initial"),
            "transitions": d.get("transitions", []),
            "terminal_states": sorted(_terminal_states(d)) if d else [],
            "instance_counts": instance_counts or {},
            "created_at": p.created_at.isoformat() if p.created_at else None}


async def create_process(db: AsyncSession, *, owner_id: str | None, name: str,
                         description: str = "", definition: dict | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ProcessError("a process name is required")
    d = validate_definition(definition)
    row = BusinessProcess(name=name[:140], description=(description or "").strip()[:500],
                          definition=d)
    row.owner_id = owner_id
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return process_out(row)


async def _load_process(db: AsyncSession, process_id: str, owner_id: str | None) -> BusinessProcess:
    p = await db.get(BusinessProcess, process_id)
    if p is None or (owner_id is not None and p.owner_id not in (owner_id, None)):
        raise ProcessError(f"process {process_id!r} not found")
    return p


async def list_processes(db: AsyncSession, owner_id: str | None) -> list[dict]:
    q = select(BusinessProcess).order_by(BusinessProcess.created_at.desc())
    if owner_id is not None:
        q = q.where(BusinessProcess.owner_id.in_((owner_id, None)))
    rows = (await db.execute(q)).scalars().all()
    out = []
    for p in rows:
        counts = await _instance_counts(db, p.id)
        out.append(process_out(p, instance_counts=counts))
    return out


async def get_process(db: AsyncSession, process_id: str, owner_id: str | None) -> dict:
    p = await _load_process(db, process_id, owner_id)
    counts = await _instance_counts(db, p.id)
    return process_out(p, instance_counts=counts)


async def _instance_counts(db: AsyncSession, process_id: str) -> dict:
    rows = (await db.execute(
        select(BusinessProcessInstance.state, func.count(BusinessProcessInstance.id))
        .where(BusinessProcessInstance.process_id == process_id)
        .group_by(BusinessProcessInstance.state))).all()
    return {state: int(n) for state, n in rows}


# ---------------------------------------------------------------------------
# instances - the tracked entities that remember
# ---------------------------------------------------------------------------

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
                         actor: str = "") -> dict:
    p = await _load_process(db, process_id, owner_id)
    ref = str(ref or "").strip()
    if not ref:
        raise ProcessError("an instance needs a ref - the external key the "
                           "business already tracks (lead id, case number, phone)")
    now = _now()
    row = BusinessProcessInstance(
        process_id=p.id, ref=ref[:180], title=(title or "").strip()[:200],
        state=(p.definition or {}).get("initial") or "",
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
        note="instance started", payload={"ref": row.ref}))
    return instance_out(row, definition=p.definition)


async def advance_instance(db: AsyncSession, instance_id: str, *, owner_id: str | None,
                           transition: str | None = None, to_state: str | None = None,
                           actor: str = "", note: str = "",
                           context_patch: dict | None = None,
                           payload: dict | None = None,
                           due_in_seconds: int | None = None) -> dict:
    """Move the instance. Resolve by transition name OR direct to_state;
    anything the machine does not allow is a loud refusal naming the
    allowed moves. The context patch MERGES into the running memory (fresh
    dicts - the JSON column is never mutated in place)."""
    row = await _load_instance(db, instance_id, owner_id)
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
    from . import system_events as events_svc

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
    return instance_out(row, definition=definition, journey=journey)


async def instance_journey(db: AsyncSession, instance_id: str) -> list[dict]:
    rows = (await db.execute(
        select(BusinessProcessTransitionLog)
        .where(BusinessProcessTransitionLog.instance_id == instance_id)
        .order_by(BusinessProcessTransitionLog.created_at.asc()))).scalars().all()
    return [_log_out(t) for t in rows]


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


# ---------------------------------------------------------------------------
# the process, measured - all derived, nothing stored twice
# ---------------------------------------------------------------------------

async def process_analytics(db: AsyncSession, process_id: str, owner_id: str | None) -> dict:
    p = await _load_process(db, process_id, owner_id)
    definition = p.definition or {}
    instances = (await db.execute(
        select(BusinessProcessInstance)
        .where(BusinessProcessInstance.process_id == p.id))).scalars().all()
    now = _now()
    terminal = _terminal_states(definition)
    by_state: dict[str, int] = {s: 0 for s in definition.get("states", [])}
    stuck: list[str] = []
    open_count = 0
    for r in instances:
        by_state[r.state] = by_state.get(r.state, 0) + 1
        if r.ended_at is None and r.state not in terminal:
            open_count += 1
            if r.due_at and now > _aware(r.due_at):
                stuck.append(r.id)
    # mean time in state per state - derived from the log's own timestamps
    logs = (await db.execute(
        select(BusinessProcessTransitionLog)
        .where(BusinessProcessTransitionLog.process_id == p.id)
        .order_by(BusinessProcessTransitionLog.created_at.asc()))).scalars().all()
    durations: dict[str, list[float]] = {}
    for r in instances:
        mine = [t for t in logs if t.instance_id == r.id]
        for i, t in enumerate(mine):
            if i + 1 < len(mine) and t.to_state:
                start = _aware(t.created_at)
                end = _aware(mine[i + 1].created_at)
                if start and end:
                    durations.setdefault(t.to_state, []).append(
                        (end - start).total_seconds())
        # the CURRENT stint for non-ended instances: still in motion
        if mine and r.ended_at is None and r.entered_state_at:
            first_at = _aware(mine[0].created_at)
            entered = _aware(r.entered_state_at)
            if first_at and entered:
                durations.setdefault(r.state, []).append(
                    (now - entered).total_seconds())
    mean_time_in_state = {
        s: round(sum(v) / len(v)) for s, v in durations.items() if v}
    advance_counts: dict[str, int] = {}
    for t in logs:
        if t.from_state is not None:
            advance_counts[t.transition] = advance_counts.get(t.transition, 0) + 1
    return {
        "process_id": p.id, "name": p.name,
        "instances": len(instances), "open": open_count,
        "by_state": by_state,
        "stuck": stuck, "stuck_count": len(stuck),
        "mean_time_in_state_seconds": mean_time_in_state,
        "advance_counts": advance_counts,
        "terminal_states": sorted(terminal),
    }
