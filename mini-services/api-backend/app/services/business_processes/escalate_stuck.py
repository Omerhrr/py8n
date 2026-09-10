"""Business processes: _systems_holding, _escalated_this_stint, escalate_stuck.

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
from .instances import _aware, advance_instance

async def _systems_holding(db: AsyncSession, process_ids: set[str]) -> dict[str, list[str]]:
    """process_id -> the lifecycles of the systems that bind it (v81 gate)."""
    if not process_ids:
        return {}
    from ...models import Py8nSystem, SystemComponent

    rows = (await db.execute(
        select(SystemComponent, Py8nSystem.lifecycle)
        .join(Py8nSystem, Py8nSystem.id == SystemComponent.system_id)
        .where(SystemComponent.kind == "process",
               SystemComponent.ref_id.in_(process_ids)))).all()
    out: dict[str, list[str]] = {}
    for comp, lifecycle in rows:
        out.setdefault(comp.ref_id, []).append(str(lifecycle or ""))
    return out


async def _escalated_this_stint(db: AsyncSession, instance_id: str,
                                entered_state_at: datetime | None) -> bool:
    """One escalation per state stint: an escalation marker on the log
    after the instance entered its current state means the door already
    knocked. Both paths mark: the machine's own ``escalate`` move (the
    advance row) and the no-move ``escalated`` record. When the team (or
    a workflow) moves the instance onward, entered_state_at advances and
    the door may knock again."""
    q = (select(BusinessProcessTransitionLog)
         .where(BusinessProcessTransitionLog.instance_id == instance_id,
                BusinessProcessTransitionLog.transition.in_(
                    ("escalated", ESCALATION_TRANSITION)))
         .order_by(BusinessProcessTransitionLog.created_at.desc())
         .limit(1))
    last = (await db.execute(q)).scalar_one_or_none()
    if last is None:
        return False
    at = _aware(last.created_at)
    entered = _aware(entered_state_at)
    if at is None or entered is None:
        return False
    return at >= entered


async def escalate_stuck(db: AsyncSession, owner_id: str | None = None, *,
                         actor: str = "scheduler",
                         now: datetime | None = None) -> dict:
    """The scheduler DOOR: sweep the open instances whose SLA promise
    (due_at) has passed while still open, and escalate each.

    A machine that wants to be escalated defines its own ``escalate``
    move (a self-loop re-arms the stint; a real move walks the entity to
    an at-risk state) - the door takes the machine's move. A machine
    without one still gets the escalation ON THE RECORD: a no-move
    'escalated' log row naming the breach. Either way the door emits
    ``business.stuck`` through the v80 event door, so workflows can
    react to the breach itself (nudge the owner, ping the channel,
    start the recovery workflow).

    v87: the escalation POLICY (channel + repeat) rides the definition.
    A machine carrying one gates its repeats through the episode
    bookkeeping (attempt 1, then one attempt per repeat_every_seconds
    until 1 + max_repeats, a state change starting a fresh episode) and
    every attempt DELIVERS over the policy's channel + lands
    ``business.escalated`` on the instance's correlation thread. A
    machine without a policy keeps the v85 semantics exactly: one knock
    per state stint, on the record, no channel.

    v89: mode="digest" replaces the N knocks with ONE summary per
    window. Per stuck instance the door only decides WHO belongs in the
    summary (fresh episode, not acked - or the snooze ran out - and
    under the cap); the bucket (one per owner+process) is due when its
    OLDEST pending item has waited digest_every_seconds, and then ONE
    ``business.escalation_digest`` event + ONE channel message lists
    them all. The machine's own escalate move is not taken in digest
    mode (a self-loop per tick would re-arm the stints the digest
    bookkeeping rides on).

    The v81 lifecycle gate holds the door for processes bound to a
    paused/stopped system; unbound processes always sweep. The clock is
    injectable (now=) for tests and replay tools.
    """
    now = _aware(now) or _now()
    q = (select(BusinessProcessInstance)
         .where(BusinessProcessInstance.ended_at.is_(None),
                BusinessProcessInstance.due_at.is_not(None)))
    if owner_id is not None:
        q = q.where(BusinessProcessInstance.owner_id.in_((owner_id, None)))
    open_rows = (await db.execute(q)).scalars().all()
    if not open_rows:
        return {"scanned": 0, "stuck": 0, "escalated": [], "recorded": [],
                "held": [], "already": 0, "digest": {"sent": [], "pending": []}}

    # terminal states per process + the lifecycle gate
    process_ids = {r.process_id for r in open_rows}
    definitions: dict[str, dict] = {}
    names: dict[str, str] = {}
    for pid in process_ids:
        p = await db.get(BusinessProcess, pid)
        if p is not None:
            definitions[pid] = p.definition or {}
            names[pid] = p.name
    lifecycles = await _systems_holding(db, process_ids)

    escalated: list[dict] = []
    recorded: list[dict] = []
    held: list[dict] = []
    already = 0
    # v89: the digest buckets - (owner_id, process_id) -> [member, ...]
    buckets: dict[tuple, list[dict]] = {}
    for row in open_rows:
        definition = definitions.get(row.process_id) or {}
        terminal = _terminal_states(definition) if definition else set()
        if terminal and row.state in terminal:
            continue  # ended in spirit - ended_at will land on the next move
        overdue_raw = (now - _aware(row.due_at)).total_seconds()
        if overdue_raw <= 0:
            continue  # the SLA still holds
        overdue = round(overdue_raw)
        cycles = lifecycles.get(row.process_id) or []
        if cycles and not any(c == "running" for c in cycles):
            held.append({"instance_id": row.id, "ref": row.ref,
                         "process_id": row.process_id,
                         "note": "bound system is not running - the door holds"})
            continue

        breach_payload = {"process_id": row.process_id,
                          "process_name": names.get(row.process_id, ""),
                          "instance_id": row.id, "ref": row.ref,
                          "title": row.title, "state": row.state,
                          "due_at": _aware(row.due_at).isoformat() if row.due_at else None,
                          "overdue_seconds": overdue}

        # v87: a policy-carrying machine gates its repeats through the
        # episode bookkeeping (cadence + cap); a machine without one keeps
        # the v85 semantics - one knock per state stint
        policy = escalations_svc.policy_from_definition(definition)

        # v89: digest mode - the door decides WHO belongs in the summary;
        # the machine's own escalate move is deliberately not taken
        if policy is not None and policy["mode"] == "digest":
            gate = escalations_svc.digest_gate(row, policy, now)
            if gate["action"] == "hold":
                held.append({"instance_id": row.id, "ref": row.ref,
                             "process_id": row.process_id,
                             "reason": gate["reason"],
                             **({k: gate[k] for k in ("attempts", "acked_by",
                                                      "snooze_until",
                                                      "snooze_remaining_seconds",
                                                      "reschedule_at",
                                                      "reschedule_remaining_seconds")
                                 if k in gate})})
                continue
            if gate.get("fresh"):
                # first observation of THIS episode: the breach itself is
                # a fact worth one event - after that the digest carries
                # the beat (no per-tick noise; that is the whole point).
                # v91: an item that switched from knock mode mid-episode
                # only anchors its digest CLOCK here (anchor_only) - the
                # breach was already announced by the knock episode's
                # business.stuck; announcing it twice is noise, not fact.
                escalations_svc.record_digest_book(db, row, count=0, now=now)
                if not gate.get("anchor_only"):
                    from .. import system_events as events_svc

                    await events_svc.emit(
                        db, row.owner_id, "business.stuck", source="business",
                        actor=actor, target_type="process_instance",
                        target_id=row.id,
                        payload={**breach_payload, "mode": "digest"},
                        correlation_id=row.id)
            buckets.setdefault((row.owner_id, row.process_id), []).append(
                {"row": row, "policy": policy, "attempt": gate["attempt"],
                 "pending_since": gate["pending_since"], "overdue": overdue,
                 "process_name": names.get(row.process_id, "")})
            continue

        attempt = 1
        if policy is not None:
            gate = escalations_svc.episode_gate(row, policy, now)
            if gate["action"] == "hold":
                held.append({"instance_id": row.id, "ref": row.ref,
                             "process_id": row.process_id,
                             "reason": gate["reason"],
                             **({k: gate[k] for k in ("attempts", "next_in_seconds",
                                                      "acked_by", "snooze_until",
                                                      "snooze_remaining_seconds",
                                                      "reschedule_at",
                                                      "reschedule_remaining_seconds")
                                 if k in gate})})
                continue
            attempt = gate["attempt"]
        elif await _escalated_this_stint(db, row.id, row.entered_state_at):
            already += 1
            continue
        if policy is None:
            # v88: a no-policy machine may still have been ACKNOWLEDGED -
            # the ack lives on the instance's memory; the door respects it
            # for the rest of the stint (one knock per stint either way -
            # this keeps the receipt visible in the door's held list)
            book = escalations_svc.episode_book(row)
            if isinstance(book.get("acked"), dict) and \
                    book.get("state") == row.state:
                held.append({"instance_id": row.id, "ref": row.ref,
                             "process_id": row.process_id,
                             "reason": "acknowledged",
                             "acked_by": str(book["acked"].get("by") or "")})
                continue

        # the machine's own move, when the machine defines one from here
        move = next((t for t in definition.get("transitions", [])
                     if t.get("name") == ESCALATION_TRANSITION
                     and t.get("from") == row.state), None)
        moved_to: str | None = None
        if move is not None:
            advanced = await advance_instance(
                db, row.id, owner_id=owner_id,
                transition=ESCALATION_TRANSITION,
                actor=actor,
                note=f"SLA breached by {overdue}s - the door escalated "
                     f"through the machine's own move",
                payload={"reason": "sla_breached", "overdue_seconds": overdue},
                due_in_seconds=None)
            moved_to = advanced["state"]
            escalated.append({"instance_id": row.id, "ref": row.ref,
                              "process_id": row.process_id,
                              "state": row.state, "moved_to": moved_to,
                              "overdue_seconds": overdue})
        else:
            db.add(BusinessProcessTransitionLog(
                process_id=row.process_id, instance_id=row.id,
                from_state=row.state, to_state=row.state,
                transition="escalated", actor=(actor or "scheduler")[:140],
                note=f"SLA breached by {overdue}s - recorded (the machine "
                     f"defines no {ESCALATION_TRANSITION!r} move from "
                     f"{row.state!r})",
                payload={"reason": "sla_breached", "overdue_seconds": overdue}))
            await db.flush()
            recorded.append({"instance_id": row.id, "ref": row.ref,
                             "process_id": row.process_id,
                             "state": row.state, "overdue_seconds": overdue})

        # the breach itself is a fact, independent of the move
        from .. import system_events as events_svc

        await events_svc.emit(
            db, row.owner_id, "business.stuck", source="business",
            actor=actor, target_type="process_instance", target_id=row.id,
            payload={**breach_payload, "escalated": moved_to is not None,
                     "moved_to": moved_to},
            correlation_id=row.id)

        # v87: a policy-carrying machine also TELLS someone - deliver over
        # the policy's channel + business.escalated on the correlation
        # thread (an honest skip IS a result), and remember the attempt
        if policy is not None:
            delivery = await escalations_svc.deliver_escalation(
                db, row, policy, process_name=names.get(row.process_id, ""),
                overdue_seconds=overdue, attempt=attempt, moved_to=moved_to,
                actor=actor, now=now)
            escalations_svc.record_episode(db, row, attempt, delivery, now)
            entry = (escalated[-1] if moved_to is not None else recorded[-1])
            entry["attempt"] = attempt
            entry["delivery"] = delivery.get("delivery", "")
            entry["delivery_detail"] = (delivery.get("detail") or "")[:300]

    # ---- v89: the digest send phase - one summary per due bucket ---------
    digest_report: dict = {"sent": [], "pending": []}
    for (b_owner, b_pid), members in buckets.items():
        policy = members[0]["policy"]
        process_name = members[0]["process_name"]
        # the bucket is due when its OLDEST pending item has waited the
        # window (pending_since stamps are aware-UTC isoformat strings,
        # so lexicographic order is chronological order)
        oldest = min(m["pending_since"] for m in members)
        oldest_dt = escalations_svc.parse_iso(oldest)
        waited = ((now - oldest_dt).total_seconds()
                  if oldest_dt is not None else policy["digest_every_seconds"])
        if waited < policy["digest_every_seconds"]:
            digest_report["pending"].append({
                "process_id": b_pid, "process_name": process_name,
                "items": len(members),
                "next_in_seconds": round(policy["digest_every_seconds"] - waited)})
            continue
        items = []
        for m in members:
            # v97: the line carries the receipt's evidence - what the team
            # already did about this item (the reschedule/snooze the door
            # holds or held) rides the summary itself
            book = escalations_svc.episode_book(m["row"])
            items.append({"instance_id": m["row"].id, "ref": m["row"].ref,
                          "title": m["row"].title, "state": m["row"].state,
                          "overdue_seconds": m["overdue"],
                          "overdue_minutes": max(0, m["overdue"] // 60),
                          "attempt": m["attempt"],
                          "reschedule_note": escalations_svc.reschedule_note(
                              book, now)})
        delivery = await escalations_svc.deliver_digest(
            db, owner_id=b_owner, policy=policy, process_id=b_pid,
            process_name=process_name, items=items, actor=actor, now=now)
        for m in members:
            escalations_svc.record_digest_book(
                db, m["row"], count=m["attempt"], now=now,
                digest_last_at=now, delivery=delivery)
            db.add(BusinessProcessTransitionLog(
                process_id=b_pid, instance_id=m["row"].id,
                from_state=m["row"].state, to_state=m["row"].state,
                transition=DIGEST_TRANSITION, actor=(actor or "scheduler")[:140],
                note=f"listed in the escalation digest "
                     f"({delivery.get('delivery', '')})",
                payload={"attempt": m["attempt"],
                         "overdue_seconds": m["overdue"]}))
        await db.flush()
        digest_report["sent"].append({
            "process_id": b_pid, "process_name": process_name,
            "items": len(items), "delivery": delivery.get("delivery", ""),
            "detail": (delivery.get("detail") or "")[:200],
            "to": delivery.get("to")})

    return {"scanned": len(open_rows),
            "stuck": (len(escalated) + len(recorded) + already
                      + sum(len(v) for v in buckets.values())),
            "escalated": escalated, "recorded": recorded,
            "held": held, "already": already, "digest": digest_report}

