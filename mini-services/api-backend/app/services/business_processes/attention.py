"""Business processes: attention_feed, system_work_surface.

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
from .chains import _ack_summary, _stuck_state
from .definitions import _allowed_from, _terminal_states
from .instances import _aware

async def attention_feed(db: AsyncSession, owner_id: str | None, *,
                         limit: int = 200,
                         now: datetime | None = None) -> dict:
    """One panel answering 'what needs a human today': every OPEN instance
    past its SLA across ALL machines, most-overdue first, each row
    carrying the escalation book the door keeps (attempts, last delivery,
    the ack) + the machine's own policy line + whether the entity was
    BORN from a cross-operator journey leg. Owner-scoped like every read;
    terminal states are skipped even when the clock ran on (a closed
    entity is not asking for attention)."""
    now = _aware(now) or _now()
    # the house pattern (same as the door): filter open + has-due-at in
    # SQL, but compare the CLOCK in Python - SQLite returns naive
    # datetimes and a naive/aware comparison in a WHERE clause lies
    q = (select(BusinessProcessInstance, BusinessProcess)
         .join(BusinessProcess,
               BusinessProcessInstance.process_id == BusinessProcess.id)
         .where(BusinessProcessInstance.ended_at.is_(None),
                BusinessProcessInstance.due_at.is_not(None)))
    if owner_id is not None:
        q = q.where(BusinessProcessInstance.owner_id.in_((owner_id, None)),
                    BusinessProcess.owner_id.in_((owner_id, None)))
    rows = (await db.execute(q)).all()
    picked: list[tuple[BusinessProcessInstance, BusinessProcess, float]] = []
    for row, proc in rows:
        due = _aware(row.due_at)
        if due is None:
            continue
        overdue = (now - due).total_seconds()
        if overdue <= 0:
            continue  # the SLA still holds
        definition = proc.definition or {}
        if row.state in _terminal_states(definition):
            continue  # a closed entity is not asking for attention
        picked.append((row, proc, overdue))
    picked.sort(key=lambda t: t[2], reverse=True)  # most overdue first
    picked = picked[:max(1, min(int(limit or 200), 500))]
    out: list[dict] = []
    for row, proc, overdue in picked:
        entered = _aware(row.entered_state_at)
        book = escalations_svc.episode_book(row)
        acked = book.get("acked") if isinstance(book.get("acked"), dict) else None
        # v96: the reschedule loan - the receipt may name the door's next
        # knock explicitly; the row carries the stamp + how long it holds
        res_at = escalations_svc.parse_iso((acked or {}).get("reschedule_at"))
        ctx = row.context or {}
        out.append({
            "process_id": proc.id, "process_name": proc.name,
            "instance_id": row.id, "ref": row.ref, "title": row.title,
            "state": row.state,
            "entered_state_at": row.entered_state_at.isoformat()
                                if row.entered_state_at else None,
            "due_at": row.due_at.isoformat() if row.due_at else None,
            "overdue_seconds": round(overdue),
            "age_in_state_seconds":
                round((now - entered).total_seconds()) if entered else 0,
            "escalation": {
                "count": int(book.get("count") or 0),
                "last_delivery": str(book.get("last_delivery") or ""),
                "last_detail": str(book.get("last_detail") or ""),
                "acked_by": str((acked or {}).get("by") or ""),
                "snooze_until": str((acked or {}).get("snooze_until") or ""),
                "reschedule_at": str((acked or {}).get("reschedule_at") or ""),
                "reschedule_remaining_seconds":
                    round((res_at - now).total_seconds())
                    if res_at is not None and res_at > now else 0,
            } if book else None,
            "escalation_summary": escalations_svc.describe_policy(
                escalations_svc.policy_from_definition(proc.definition or {})),
            "journey_leg": bool(ctx.get("journey")),
        })
    return {"attention": out, "count": len(out),
            "machines": len({r["process_id"] for r in out}),
            "now": now.isoformat()}


async def system_work_surface(db: AsyncSession, process_ids: list[str], *,
                              limit: int = 50,
                              now: datetime | None = None) -> dict:
    """v105: the system's own work surface - what needs doing on the
    machines THIS system binds, composed for the front door.

    A company's people land on their own address and see their own
    pending work, not the builder's estate: every bound machine with its
    live operation (open / stuck, the same derivation every view uses)
    and the attention rows - open instances past their SLA, most-overdue
    first, each carrying the escalation book the door keeps (is it
    knocking, has a human taken it, is a loan holding). The SAME
    predicate the estate feed runs, scoped to the system's machines:
    terminal states are skipped (a closed entity is not asking for
    attention) and the clock is compared in Python - SQLite returns
    naive datetimes and a naive/aware comparison lies (v38 GOTCHA).

    v106: every attention row also carries the moves the machine allows
    FROM its current state (``transitions`` - name + to), resolved by
    the SAME ``_allowed_from`` the advance door runs, so the surface can
    put advance actions beside the acks and the buttons can never offer
    a move the door would refuse.

    v107: every machine also carries its own VIEW (``instances``) - the
    open work on ITS board, the stuck rising to the top, capped at 8
    with ``instances_hidden`` naming what did not fit - so the front
    door renders per-machine views beneath the attention list: the
    machine's own people see their department's pending rows without
    waiting for an SLA breach."""
    now = _aware(now) or _now()
    ids = [p for p in (process_ids or []) if p]
    empty = {"machines": [], "attention": [],
             "totals": {"machines": 0, "open": 0, "stuck": 0, "attention": 0},
             "now": now.isoformat()}
    if not ids:
        return empty
    procs = (await db.execute(
        select(BusinessProcess).where(BusinessProcess.id.in_(ids))
        .order_by(BusinessProcess.created_at.asc()))).scalars().all()
    if not procs:
        return empty
    by_id = {p.id: p for p in procs}
    definitions = {p.id: (p.definition or {}) for p in procs}
    inst_rows = (await db.execute(
        select(BusinessProcessInstance)
        .where(BusinessProcessInstance.process_id.in_(ids))
        .order_by(BusinessProcessInstance.created_at.desc()))).scalars().all()
    counts = {p.id: {"open": 0, "stuck": 0} for p in procs}
    # v107: the per-machine VIEW - each machine carries the open work on
    # ITS OWN board (newest first, the stuck rising to the top), capped
    # so one chatty machine cannot drown the surface.
    PER_MACHINE_VIEW_CAP = 8
    mach_rows: dict[str, list[tuple[BusinessProcessInstance, bool, int]]] = {
        p.id: [] for p in procs}
    picked: list[tuple[BusinessProcessInstance, BusinessProcess, float]] = []
    for r in inst_rows:
        proc = by_id.get(r.process_id)
        if proc is None:
            continue
        stuck, overdue = _stuck_state(r, definitions.get(r.process_id) or {}, now)
        if r.ended_at is not None:
            continue  # terminal rows sit in the journey, not the work list
        counts[r.process_id]["open"] += 1
        mach_rows[r.process_id].append((r, stuck, overdue))
        if stuck:
            counts[r.process_id]["stuck"] += 1
            picked.append((r, proc, overdue))
    picked.sort(key=lambda t: t[2], reverse=True)  # most overdue first
    picked = picked[:max(1, min(int(limit or 50), 200))]
    attention_rows: list[dict] = []
    for r, proc, overdue in picked:
        book = escalations_svc.episode_book(r)
        acked = _ack_summary(book, now) if book else None
        res_at = escalations_svc.parse_iso(
            (book.get("acked") or {}).get("reschedule_at")
            if isinstance(book.get("acked"), dict) else None)
        # v106: the moves the machine allows FROM this row's state - the
        # same resolution the advance door runs (_allowed_from), so the
        # surface's buttons and the door's rules can never drift. A state
        # with no outgoing moves wears an empty list (the surface hides
        # the control - an honest absence, never a dead button).
        definition = definitions.get(proc.id) or {}
        attention_rows.append({
            "process_id": proc.id, "process_name": proc.name,
            "instance_id": r.id, "ref": r.ref, "title": r.title,
            "state": r.state,
            "entered_state_at": r.entered_state_at.isoformat()
                                if r.entered_state_at else None,
            "due_at": r.due_at.isoformat() if r.due_at else None,
            "overdue_seconds": round(overdue),
            "transitions": [{"name": t["name"], "to": t["to"]}
                            for t in _allowed_from(definition, r.state)],
            "escalation": {
                "count": int(book.get("count") or 0),
                "acked": acked,
                "reschedule_at": str((book.get("acked") or {}).get(
                    "reschedule_at") or "")
                if isinstance(book.get("acked"), dict) else "",
                "reschedule_remaining_seconds":
                    round((res_at - now).total_seconds())
                    if res_at is not None and res_at > now else 0,
            } if book else None,
        })
    machines_out: list[dict] = []
    open_total = stuck_total = 0
    for p in procs:
        c = counts[p.id]
        open_total += c["open"]
        stuck_total += c["stuck"]
        # v107: the machine's own view beneath the attention list - its
        # open instances on ITS board: the stuck rise to the top (most
        # overdue first among them), the rest keep the newest-first
        # order they arrived in; capped, with the honest "and N more"
        # so a chatty machine is summarized, never hidden.
        rows = sorted(mach_rows.get(p.id) or [], key=lambda t: 0 if t[1] else 1)
        shown = rows[:PER_MACHINE_VIEW_CAP]
        machines_out.append({
            "process_id": p.id, "name": p.name,
            "open": c["open"], "stuck": c["stuck"],
            "escalation_summary": escalations_svc.describe_policy(
                escalations_svc.policy_from_definition(definitions[p.id])),
            "instances": [{
                "instance_id": r.id, "ref": r.ref, "title": r.title,
                "state": r.state,
                "due_at": r.due_at.isoformat() if r.due_at else None,
                "overdue_seconds": round(overdue),
                "stuck": stuck,
            } for r, stuck, overdue in shown],
            "instances_hidden": max(0, c["open"] - len(shown)),
        })
    return {"machines": machines_out, "attention": attention_rows,
            "totals": {"machines": len(procs), "open": open_total,
                       "stuck": stuck_total, "attention": len(attention_rows)},
            "now": now.isoformat()}

