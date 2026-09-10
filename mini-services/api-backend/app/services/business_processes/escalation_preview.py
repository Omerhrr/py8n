"""Business processes: escalation_preview.

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
from .escalate_stuck import _escalated_this_stint
from .instances import _aware
from .process_crud import _load_process

async def escalation_preview(db: AsyncSession, process_id: str, *,
                             owner_id: str | None, policy: dict | None,
                             now: datetime | None = None) -> dict:
    """v93: what the door would do RIGHT NOW under the DRAFT policy -
    rendered for the board's policy editor, never delivered, nothing
    recorded. The draft goes through the SAME validator a save would
    (a broken draft refuses loudly here, before it can be saved), then
    every open instance past its SLA on this machine is walked through
    the draft's own gate:

    * mode=knock: each episode the gate would knock on becomes a rendered
      message (the draft's template or the default, subject included for
      the long-form channels) with its attempt number and rotation target;
    * mode=digest: the candidates form the bucket, the bucket's window is
      compared against its oldest pending item, and the ONE summary -
      subject + body, the exact strings the email adapter would carry -
      is rendered whether the window has elapsed or not (with the honest
      ``next_in_seconds`` when it has not);
    * no policy: the v85 semantics - one knock per stint, event-only.

    The holds are half the truth: acknowledged episodes (snooze remaining
    named), too-soon cadences (next attempt in ...), completed episodes
    (the cap) all show up as HELD, so the editor can explain why the
    quiet. Delivery honesty rides along: an event-only draft, a missing
    target or an unbound channel endpoint is a skip the preview NAMES -
    the same skip the door would record. Pure read + render: no book
    written, no event emitted, no message sent."""
    p = await _load_process(db, process_id, owner_id)
    now = _aware(now) or _now()
    try:
        clean = escalations_svc.validate_escalation_policy(policy)
    except escalations_svc.EscalationPolicyError as exc:
        raise ProcessError(str(exc)) from exc

    definition = p.definition or {}
    terminal = _terminal_states(definition)
    q = (select(BusinessProcessInstance)
         .where(BusinessProcessInstance.process_id == p.id,
                BusinessProcessInstance.ended_at.is_(None),
                BusinessProcessInstance.due_at.is_not(None)))
    rows = (await db.execute(q)).scalars().all()
    overdue_rows: list[tuple[BusinessProcessInstance, float]] = []
    for row in rows:
        due = _aware(row.due_at)
        if due is None or row.state in terminal:
            continue
        overdue = (now - due).total_seconds()
        if overdue > 0:
            overdue_rows.append((row, round(overdue)))
    overdue_rows.sort(key=lambda t: t[1], reverse=True)

    # delivery honesty - the same doors the real send walks through
    channel = (clean or {}).get("channel", "")
    target = escalations_svc.rotation_target(clean, 1) if clean else ""
    endpoint_bound = (await escalations_svc._resolve_endpoint(db, p.owner_id, channel)
                      is not None) if (clean and channel and target) else False
    would_deliver = bool(clean and channel and target and endpoint_bound)
    if not clean:
        delivery_note = ("no policy - the door keeps the v85 semantics: one "
                         "escalation per state stint, event-only")
    elif not channel:
        delivery_note = ("event-only - business.stuck fires on the event "
                         "timeline, nobody is knocked")
    elif not target:
        delivery_note = (f"would skip - no target configured (bind 'to' or a "
                         f"handlers roster to deliver over {channel})")
    elif not endpoint_bound:
        delivery_note = (f"would skip - no {channel} channel endpoint bound; "
                         "the message renders but nothing is delivered")
    else:
        delivery_note = f"would deliver over {channel} -> {target}"

    messages: list[dict] = []
    held: list[dict] = []
    bucket: list[dict] = []
    for row, overdue in overdue_rows:
        base = {"instance_id": row.id, "ref": row.ref, "title": row.title,
                "state": row.state, "overdue_seconds": overdue,
                "overdue_minutes": max(0, overdue // 60)}
        if clean is None:
            book = escalations_svc.episode_book(row)
            if (book.get("state") == row.state
                    and isinstance(book.get("acked"), dict)):
                # v88: the ack holds even on a no-policy machine
                held.append({**base, "reason": "acknowledged",
                             "acked_by": str(book["acked"].get("by") or "")})
            elif await _escalated_this_stint(db, row.id, row.entered_state_at):
                held.append({**base, "reason": "already_escalated_this_stint",
                             "note": "one knock per stint - a state change re-arms the door"})
            else:
                messages.append({**base, "attempt": 1, "to": "",
                                 "subject": escalations_svc.knock_subject(
                                     process_name=p.name, ref=row.ref,
                                     title=row.title, state=row.state, attempt=1),
                                 "message": escalations_svc.render_message(
                                     escalations_svc.DEFAULT_TEMPLATE,
                                     process_name=p.name, ref=row.ref,
                                     title=row.title, state=row.state,
                                     overdue_minutes=max(0, overdue // 60),
                                     attempt=1)})
            continue
        if clean["mode"] == "digest":
            gate = escalations_svc.digest_gate(row, clean, now)
            if gate["action"] == "hold":
                held.append({**base, "reason": gate["reason"],
                             **{k: gate[k] for k in
                                ("attempts", "acked_by", "snooze_until",
                                 "snooze_remaining_seconds", "next_in_seconds",
                                 "reschedule_at", "reschedule_remaining_seconds")
                                if k in gate}})
                continue
            bucket.append({**base, "attempt": gate["attempt"],
                           "pending_since": gate["pending_since"],
                           "waited_seconds": gate["waited_seconds"]})
            continue
        gate = escalations_svc.episode_gate(row, clean, now)
        if gate["action"] == "hold":
            held.append({**base, "reason": gate["reason"],
                         **{k: gate[k] for k in
                            ("attempts", "acked_by", "snooze_until",
                             "snooze_remaining_seconds", "next_in_seconds",
                             "reschedule_at", "reschedule_remaining_seconds")
                            if k in gate}})
            continue
        attempt = gate["attempt"]
        to = escalations_svc.rotation_target(clean, attempt)
        messages.append({**base, "attempt": attempt, "to": to,
                         "subject": escalations_svc.knock_subject(
                             process_name=p.name, ref=row.ref,
                             title=row.title, state=row.state, attempt=attempt),
                         "message": escalations_svc.render_message(
                             clean["message_template"] or escalations_svc.DEFAULT_TEMPLATE,
                             process_name=p.name, ref=row.ref, title=row.title,
                             state=row.state, overdue_minutes=max(0, overdue // 60),
                             attempt=attempt)})

    digest_out: dict | None = None
    if clean is not None and clean["mode"] == "digest":
        window = clean["digest_every_seconds"]
        oldest_waited = max((b["waited_seconds"] for b in bucket), default=0)
        items = []
        for b in bucket:
            # v97: the preview's digest carries the same reschedule evidence
            # the real render would - the editor sees the whole truth
            brow = next((r for r, _od in overdue_rows if r.id == b["instance_id"]), None)
            bbook = escalations_svc.episode_book(brow) if brow is not None else {}
            items.append({"instance_id": b["instance_id"], "ref": b["ref"],
                          "title": b["title"], "state": b["state"],
                          "overdue_seconds": b["overdue_seconds"],
                          "overdue_minutes": b["overdue_minutes"],
                          "attempt": b["attempt"],
                          "reschedule_note": escalations_svc.reschedule_note(
                              bbook, now)})
        subject = escalations_svc.digest_subject(process_name=p.name,
                                                 item_count=len(items))
        body = (escalations_svc.render_digest(process_name=p.name, items=items)
                if items else
                f"[py8n] Escalation digest - {p.name}: nothing past SLA right "
                "now - the next window would say so.")
        digest_out = {
            "subject": subject, "body": body, "items": items,
            "candidates": len(items),
            "window_seconds": window,
            "oldest_waited_seconds": oldest_waited,
            "due": bool(items) and oldest_waited >= window,
            "next_in_seconds": (max(0, window - oldest_waited)
                                if items else 0),
        }

    return {
        "process_id": p.id, "process_name": p.name,
        "policy": clean,
        "policy_line": escalations_svc.describe_policy(clean),
        "overdue_count": len(overdue_rows),
        "mode": (clean or {}).get("mode") or "event-only",
        "would_deliver": would_deliver,
        "delivery_note": delivery_note,
        "messages": messages,
        "digest": digest_out,
        "held": held,
        "now": now.isoformat(),
    }

