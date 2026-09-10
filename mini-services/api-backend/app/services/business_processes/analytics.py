"""Business processes: process_analytics.

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
from .definitions import _terminal_states
from .instances import _aware
from .process_crud import _load_process

async def process_analytics(db: AsyncSession, process_id: str, owner_id: str | None,
                            *, now: datetime | None = None) -> dict:
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
    escalations = 0
    annotations = 0
    acknowledgements = 0
    digests = 0
    for t in logs:
        if t.from_state is not None:
            advance_counts[t.transition] = advance_counts.get(t.transition, 0) + 1
        # v86 fix found live: the door's nudge lands BOTH ways - the
        # no-move 'escalated' rows AND the machine's own 'escalate'
        # move. The metric counts the nudge, not the paperwork.
        if t.transition in ("escalated", "escalate"):
            escalations += 1
        # v88: the agents' annotations and the team's acknowledgements
        # are counted on their own - they are facts and receipts, not
        # moves, so they leave advance_counts (see PAPERWORK_TRANSITIONS)
        if t.transition == ANNOTATE_TRANSITION:
            annotations += 1
        if t.transition == ACK_TRANSITION:
            acknowledgements += 1
        # v89: the digest receipts - a 'digest' row says this entity was
        # listed in a summary that went out
        if t.transition == DIGEST_TRANSITION:
            digests += 1
    for name in PAPERWORK_TRANSITIONS:
        advance_counts.pop(name, None)

    # v94: the escalation HISTORY - the door's rhythm over the last 14
    # days, one bucket per day, derived entirely from the log's own
    # timestamps (the same rows the counts above read): the door's
    # knock/move ('escalated' + the machine's own 'escalate'), the
    # human's receipts (acknowledgements) and the summaries (digests).
    # The window is NAMED so a quiet stretch reads as data, not absence.
    clock = _aware(now) or _now()
    window_days = 14
    day_keys = [(clock - timedelta(days=i)).date().isoformat()
                for i in range(window_days - 1, -1, -1)]
    hist: dict[str, dict] = {k: {"escalations": 0, "acknowledgements": 0,
                                 "digests": 0} for k in day_keys}
    for t in logs:
        created = _aware(t.created_at)
        if created is None:
            continue
        bucket = hist.get(created.date().isoformat())
        if bucket is None:
            continue  # older than the window - the sparkline is honest
        if t.transition in ("escalated", "escalate"):
            bucket["escalations"] += 1
        elif t.transition == ACK_TRANSITION:
            bucket["acknowledgements"] += 1
        elif t.transition == DIGEST_TRANSITION:
            bucket["digests"] += 1
    escalation_history = {
        "window_days": window_days,
        "days": [{"date": k, **hist[k], "total": sum(hist[k].values())}
                 for k in day_keys],
    }
    return {
        "process_id": p.id, "name": p.name,
        "instances": len(instances), "open": open_count,
        "by_state": by_state,
        "stuck": stuck, "stuck_count": len(stuck),
        "escalations": escalations,
        "annotations": annotations,
        "acknowledgements": acknowledgements,
        "digests": digests,
        "escalation_history": escalation_history,
        "mean_time_in_state_seconds": mean_time_in_state,
        "advance_counts": advance_counts,
        "terminal_states": sorted(terminal),
    }

