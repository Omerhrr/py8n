"""Business processes: acknowledge_escalation.

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
from .escalate_stuck import _escalated_this_stint
from .instances import _load_instance, instance_journey, instance_out
from .process_crud import _load_process

async def acknowledge_escalation(db: AsyncSession, process_id: str,
                                 instance_id: str, *, owner_id: str | None,
                                 by: str, note: str = "",
                                 snooze_hours: float | None = None,
                                 reschedule_in_minutes: float | None = None) -> dict:
    """A human takes the escalation: the episode goes quiet (episode_gate
    holds with reason 'acknowledged'), the receipt is ON THE RECORD (an
    'escalation_acknowledged' journey row naming who) and
    ``business.escalation_acknowledged`` lands on the instance's
    correlation thread - so a workflow can react to the take (ack -> open
    the follow-up task, post to the channel).

    v89: snooze_hours turns the hold into a LOAN - the receipt carries a
    snooze_until stamp and the door RE-KNOCKS once it runs out (an ack
    without one still owns the rest of the state stint, v88 semantics).
    Re-acking replaces the loan - snoozing again extends it.

    v96: reschedule_in_minutes names the door's next knock EXPLICITLY -
    the receipt carries a reschedule_at stamp (the human picked the
    moment, not a duration) and the door re-knocks when it passes. One
    clock per receipt: snooze_hours AND a reschedule together refuse
    loud - the door does not guess which loan it holds.

    The door has to have knocked first: an episode bookkeeping with at
    least one attempt in the current stint, or an escalation marker on
    the journey this stint - acknowledging silence is a loud refusal.
    A state change starts a fresh episode and the door may knock again."""
    row = await _load_instance(db, instance_id, owner_id)
    if row.process_id != process_id:
        raise ProcessError(f"instance {instance_id!r} is not in process {process_id!r}")
    p = await _load_process(db, process_id, owner_id)
    who = (by or "").strip()
    if not who:
        raise ProcessError("an acknowledgement names who acknowledged (by) - "
                           "a receipt without a name is not a receipt")
    if snooze_hours is not None:
        try:
            snooze_hours = float(snooze_hours)
        except (TypeError, ValueError):
            raise ProcessError("snooze_hours must be a number of hours") from None
        if snooze_hours < 0:
            raise ProcessError("snooze_hours must be >= 0 (0 = the door keeps "
                               "its cadence; omit it to own the rest of the stint)")
    if reschedule_in_minutes is not None:
        try:
            reschedule_in_minutes = float(reschedule_in_minutes)
        except (TypeError, ValueError):
            raise ProcessError("reschedule_in_minutes must be a number of "
                               "minutes") from None
        if reschedule_in_minutes < 0:
            raise ProcessError("reschedule_in_minutes must be >= 0 (0 = the "
                               "door re-knocks on its next sweep)")
    if snooze_hours is not None and reschedule_in_minutes is not None:
        raise ProcessError("name ONE loan - snooze_hours OR "
                           "reschedule_in_minutes, not both (the door refuses "
                           "to guess which clock holds)")
    book = escalations_svc.episode_book(row)
    has_episode = (int(book.get("count") or 0) >= 1
                   and book.get("state") == row.state)
    if not has_episode:
        has_episode = await _escalated_this_stint(db, row.id, row.entered_state_at)
    if not has_episode:
        raise ProcessError("no escalation episode to acknowledge - the door "
                           "has not knocked for this stint (state "
                           f"{row.state!r})")
    ack = escalations_svc.record_ack(db, row, by=who, note=note,
                                     snooze_hours=snooze_hours,
                                     reschedule_in_minutes=reschedule_in_minutes,
                                     now=_now())
    payload = {"attempt": int(book.get("count") or 0)}
    if ack.get("snooze_until"):
        payload["snooze_hours"] = ack["snooze_hours"]
        payload["snooze_until"] = ack["snooze_until"]
    if ack.get("reschedule_at"):
        payload["reschedule_in_minutes"] = ack["reschedule_in_minutes"]
        payload["reschedule_at"] = ack["reschedule_at"]
    db.add(BusinessProcessTransitionLog(
        process_id=p.id, instance_id=row.id, from_state=row.state,
        to_state=row.state, transition=ACK_TRANSITION,
        actor=who[:140], note=(note or "").strip()[:500]
        or "escalation acknowledged - the episode goes quiet",
        payload=payload))
    await db.flush()

    from .. import system_events as events_svc

    await events_svc.emit(
        db, row.owner_id, "business.escalation_acknowledged", source="business",
        actor=who, target_type="process_instance", target_id=row.id,
        payload={"process_id": p.id, "process_name": p.name,
                 "instance_id": row.id, "ref": row.ref, "title": row.title,
                 "state": row.state, "acknowledged_by": who,
                 "note": (note or "")[:200],
                 **({k: ack[k] for k in ("snooze_hours", "snooze_until",
                                         "reschedule_in_minutes",
                                         "reschedule_at")
                     if k in ack})},
        correlation_id=row.id)
    return {"instance": instance_out(row, definition=p.definition,
                                     journey=await instance_journey(db, row.id)),
            "ack": ack}

