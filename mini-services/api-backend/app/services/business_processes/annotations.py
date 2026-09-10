"""Business processes: annotate_instance.

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
from .instances import _load_instance, instance_journey, instance_out
from .process_crud import _load_process

async def annotate_instance(db: AsyncSession, process_id: str, instance_id: str,
                            *, owner_id: str | None, context_patch: dict,
                            actor: str = "", note: str = "") -> dict:
    """Write facts DIRECTLY into a tracked entity's running memory - no
    state change, no move, the machine untouched.

    business_query (v87) is the read door; business_advance (v85) moves
    the machine; THIS is the memory door: an agent that learned something
    mid-flight (the budget number from a call, the new address from a
    text, the sentiment from a transcript) lands it on the entity the
    whole operation reads. The patch MERGES like every context write
    (fresh dicts - the JSON column is never mutated in place); the write
    is ON THE RECORD (an 'annotate' journey row naming the keys) and
    emits ``business.annotated`` on the instance's correlation thread -
    so a workflow can react to a FACT landing (annotate -> the reactive
    advance that the fact justifies).

    Loud refusals: an empty or non-object patch, and any write under the
    reserved ``escalations`` key - that corner of the memory is the
    escalation door's episode bookkeeping, not the agents' scratch space.
    Terminal instances still accept annotations: the journey may be
    complete, but facts keep landing (the real contract value arrives
    after the deal is won)."""
    row = await _load_instance(db, instance_id, owner_id)
    if row.process_id != process_id:
        raise ProcessError(f"instance {instance_id!r} is not in process {process_id!r}")
    p = await _load_process(db, process_id, owner_id)
    if not isinstance(context_patch, dict) or not context_patch:
        raise ProcessError("annotate needs facts: context_patch must be a "
                           "non-empty object of {key: value}")
    clash = sorted(str(k) for k in context_patch if str(k) in RESERVED_CONTEXT_KEYS)
    if clash:
        raise ProcessError(f"context key(s) {clash} are reserved (the escalation "
                           "door's episode bookkeeping) - annotate facts under "
                           "other keys")
    new_ctx = dict(row.context or {})
    new_ctx.update(context_patch)
    row.context = new_ctx
    db.add(row)
    who = (actor or "agent").strip() or "agent"
    keys = sorted(str(k) for k in context_patch)
    db.add(BusinessProcessTransitionLog(
        process_id=p.id, instance_id=row.id, from_state=row.state,
        to_state=row.state, transition=ANNOTATE_TRANSITION,
        actor=who[:140],
        note=(note or "").strip()[:500] or f"facts annotated: {keys}",
        payload={"keys": keys}))
    await db.flush()

    from .. import system_events as events_svc

    await events_svc.emit(
        db, row.owner_id, "business.annotated", source="business",
        actor=who, target_type="process_instance", target_id=row.id,
        payload={"process_id": p.id, "process_name": p.name,
                 "instance_id": row.id, "ref": row.ref, "title": row.title,
                 "state": row.state, "keys": keys, "note": (note or "")[:200]},
        correlation_id=row.id)
    return instance_out(row, definition=p.definition,
                        journey=await instance_journey(db, row.id))

