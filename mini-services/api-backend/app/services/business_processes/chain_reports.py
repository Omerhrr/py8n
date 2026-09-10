"""Business processes: parse_report_recipients, parse_report_chains, _chain_report_out, chain_report_subject, get_chain_report, upsert_chain_report, delete_chain_report, dispatch_chain_report, dispatch_due_chain_reports, send_chain_report_now, escalation_history_grid, escalation_day_detail.

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
from .chains import chain_history_csv
from .instances import _aware
from .process_crud import _load_process

def parse_report_recipients(raw: str) -> list[str]:
    """v100: the recipient LIST out of whatever the form carried - commas,
    semicolons and newlines all separate (an address never contains any of
    them), whitespace stripped, empties dropped, duplicates collapsed
    case-insensitively (first spelling wins). Order is preserved: the
    envelope names them the way the operator typed them."""
    seen: set[str] = set()
    out: list[str] = []
    for piece in str(raw or "").replace("\n", ",").replace(";", ",").split(","):
        addr = piece.strip()
        if not addr:
            continue
        if addr.lower() in seen:
            continue
        seen.add(addr.lower())
        out.append(addr)
    return out


def parse_report_chains(raw: str, *, ceiling: int | None = CHAIN_REPORT_MAX_CHAINS) -> list[str]:
    """v101: the chain TAG LIST out of whatever the form carried - the same
    discipline the recipient list obeys: commas, semicolons and newlines
    all separate, whitespace stripped, empties dropped, duplicates collapsed
    case-insensitively (first spelling wins), order preserved. The ceiling
    is loud (8 - a report is a staff brief; pass ``ceiling=None`` when the
    caller just wants the parse, the way the CSV endpoint does)."""
    seen: set[str] = set()
    out: list[str] = []
    for piece in str(raw or "").replace("\n", ",").replace(";", ",").split(","):
        name = piece.strip()
        if not name:
            continue
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append(name)
    if ceiling is not None and len(out) > ceiling:
        raise ProcessError(
            f"a chain report watches at most {ceiling} chains (got {len(out)}) "
            "- name the ones that matter, or leave the scope empty and the "
            "whole estate map rides")
    return out


def _chain_report_out(row: ChainReportSchedule) -> dict:
    """The schedule as the API serves it - the state the board reads
    (v100: the parsed recipient LIST and its count ride along, so the
    board can say "one file, N names" without re-parsing; v101: the
    chain TAG LIST too, plus the named cadence when the seconds match
    one of the schedule's own rhythms)."""
    recips = parse_report_recipients(row.to or "")
    chains_scope = parse_report_chains(
        getattr(row, "chains", None) or "", ceiling=None)
    if not chains_scope and (row.chain or "").strip():
        chains_scope = [row.chain.strip()]  # pre-v101 rows, defensively
    cadence_name = ""
    try:
        cadence_name = next((name for name, secs in
                             CHAIN_REPORT_CADENCES.items()
                             if secs == int(row.cadence_seconds)), "")
    except (TypeError, ValueError):
        pass
    return {
        "id": row.id, "owner_id": row.owner_id, "enabled": bool(row.enabled),
        "cadence_seconds": int(row.cadence_seconds),
        "cadence": cadence_name,
        "to": row.to or "", "recipients": recips,
        "recipient_count": len(recips),
        "history_limit": int(row.history_limit),
        "chain": row.chain or "", "system": row.system or "",
        "chains": chains_scope, "chain_count": len(chains_scope),
        "last_sent_at": row.last_sent_at.isoformat() if row.last_sent_at else None,
        "next_due": row.next_due.isoformat() if row.next_due else None,
        "last_result": row.last_result or None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def chain_report_subject(legs: int, rides: int, chain: str = "",
                         system: str = "", *, chains: list[str] | None = None,
                         cadence: str = "") -> str:
    """The scan line the subject carries (the digest's own pattern):
    '[py8n] Chain history - N ride(s) across M leg(s)' - the chain(s)
    and/or the system named when the report watches them (v101: the tag
    list reads 'Chains: A, B' while one name keeps the v99 shape; the
    WEEKLY rhythm renames the scan line to the weekly digest's own -
    the same envelope, the same list, the file on its weekly beat)."""
    named = (cadence or "").strip().lower()
    base_name = ("[py8n] Weekly chain digest" if named == "weekly"
                 else "[py8n] Chain history")
    base = f"{base_name} - {rides} ride(s) across {legs} leg(s)"
    scope = [str(c).strip() for c in (chains or []) if str(c).strip()]
    if len(scope) > 1:
        chain_scope = f"Chains: {', '.join(scope)}"  # the tag list, named
    elif scope:
        chain_scope = scope[0]  # one chain keeps the v99 suffix shape
    else:
        chain_scope = chain.strip()
    suffixes = [s for s in (chain_scope,
                            f"System: {system.strip()}" if system.strip() else "")
                if s]
    return f"{base} - {' - '.join(suffixes)}" if suffixes else base


async def get_chain_report(db: AsyncSession, owner_id: str | None) -> dict:
    """The owner's chain-report schedule, or the honest absence - there is
    nothing to configure until the owner asks for it."""
    q = select(ChainReportSchedule)
    if owner_id is not None:
        q = q.where(ChainReportSchedule.owner_id == owner_id)
    else:
        q = q.where(ChainReportSchedule.owner_id.is_(None))
    row = (await db.execute(q)).scalars().first()
    return {"schedule": _chain_report_out(row) if row else None}


async def upsert_chain_report(db: AsyncSession, owner_id: str | None, *,
                              enabled: bool = True,
                              cadence_seconds: int = 86400,
                              to: str = "",
                              history_limit: int = 50,
                              chain: str = "",
                              system: str = "",
                              chains: str = "",
                              cadence: str = "") -> dict:
    """Create or update the owner's ONE chain-report schedule - the same
    loud validation every definition carries: cadence at the floor (a
    file dispatch is a minutes concern), a real RECIPIENT LIST (v100:
    commas/semicolons separate the names; every name carries an "@",
    duplicates collapse, the ceiling is loud), the depth clamped to the
    map's own 1..50, and the SCOPE (v101: a chain TAG LIST - the same
    parse the recipients obey, the ceiling loud; the legacy single
    ``chain`` folds into the list when no list is given, one name keeps
    the legacy column truthful so v99/v100 readers never drift). The
    named RHYTHM (v101: hourly | daily | weekly) rules the seconds when
    spoken - the weekly digest rides the SAME envelope path to the
    report's own list, only the beat is slower. The new cadence rules
    the NEXT window: next_due re-anchors to now + cadence the way the
    digest's window re-anchors on a mode switch."""
    recips = parse_report_recipients(to)
    if not recips:
        raise ProcessError("a chain report names its recipient (to) - an "
                           "email address, or the file has nowhere to land")
    bad = [a for a in recips if "@" not in a]
    if bad:
        raise ProcessError(f"recipient {bad[0]!r} is not an email address "
                           "(the list is comma-separated, every name carries "
                           "an @)")
    if len(recips) > CHAIN_REPORT_MAX_RECIPIENTS:
        raise ProcessError(f"a chain report carries at most "
                           f"{CHAIN_REPORT_MAX_RECIPIENTS} recipients "
                           f"(got {len(recips)}) - a report is a staff "
                           "brief, not a mailing list")
    try:
        cadence_seconds = int(cadence_seconds)
    except (TypeError, ValueError):
        raise ProcessError("cadence_seconds must be a number of seconds") from None
    named = (cadence or "").strip().lower()
    if named:
        if named not in CHAIN_REPORT_CADENCES:
            raise ProcessError(
                f"unknown cadence {named!r} - the named rhythms are "
                f"{', '.join(sorted(CHAIN_REPORT_CADENCES))} (or speak "
                "cadence_seconds directly)")
        cadence_seconds = CHAIN_REPORT_CADENCES[named]
    if cadence_seconds < CHAIN_REPORT_MIN_CADENCE:
        raise ProcessError(f"cadence_seconds must be >= "
                           f"{CHAIN_REPORT_MIN_CADENCE} (a file dispatch is a "
                           "minutes concern - the door will not spam)")
    try:
        history_limit = max(1, min(int(history_limit), 50))
    except (TypeError, ValueError):
        history_limit = 50
    chain = (chain or "").strip()[:120]
    system = (system or "").strip()[:120]
    # v101: the chain TAG LIST - the same parse the recipients obey, the
    # ceiling loud; the legacy single chain folds in when no list came
    scope = parse_report_chains(chains)
    if not scope and chain:
        scope = [chain]
    scope = [name[:120] for name in scope]

    q = select(ChainReportSchedule)
    if owner_id is not None:
        q = q.where(ChainReportSchedule.owner_id == owner_id)
    else:
        q = q.where(ChainReportSchedule.owner_id.is_(None))
    row = (await db.execute(q)).scalars().first()
    now = _now()
    if row is None:
        row = ChainReportSchedule(owner_id=owner_id, created_at=now)
        db.add(row)
    row.enabled = bool(enabled)
    row.cadence_seconds = cadence_seconds
    row.to = ", ".join(recips)
    row.history_limit = history_limit
    row.chain = scope[0] if len(scope) == 1 else ""
    row.system = system
    row.chains = ", ".join(scope)
    row.next_due = now + timedelta(seconds=cadence_seconds)
    row.updated_at = now
    await db.flush()
    return {"schedule": _chain_report_out(row)}


async def delete_chain_report(db: AsyncSession, owner_id: str | None) -> dict:
    """Remove the schedule - the v85 semantics return: the file stops
    riding the door entirely (removing is not pausing)."""
    q = select(ChainReportSchedule)
    if owner_id is not None:
        q = q.where(ChainReportSchedule.owner_id == owner_id)
    else:
        q = q.where(ChainReportSchedule.owner_id.is_(None))
    row = (await db.execute(q)).scalars().first()
    if row is None:
        raise ProcessError("no chain-report schedule to remove")
    await db.delete(row)
    await db.flush()
    return {"removed": True}


async def dispatch_chain_report(db: AsyncSession, row: ChainReportSchedule, *,
                                now: datetime) -> dict:
    """One dispatch: render the SAME file the plot exports
    (chain_history_csv, zero drift - the schedule's own depth and its
    chain/system scopes), then deliver it over the owner's bound EMAIL
    endpoint as a real MIME attachment. v100: ONE envelope carries EVERY
    recipient the schedule names (one SMTP conversation, the names on the
    To header and on the envelope). v101: the scope is the chain TAG
    LIST - the file covers every chain the list names (composing with
    the system scope) - and the body breaks the rides down BY chain as
    well as BY system.

    The named rhythm (hourly | daily | weekly) only renames the scan
    line and adds the Rhythm line: the weekly digest rides the SAME
    envelope path to the report's OWN list - same endpoint, same
    recipients, same attachment, same conversation - only the beat is
    slower. Nothing about the wire changes, because a weekly digest is
    the report on its weekly beat, not a different message.

    Every outcome is an honest record - delivered, skipped (no endpoint,
    no recipient, an empty window) or failed (smtp refused) - and the
    schedule stamps them all.

    The digest's own discipline, applied to the file: a due attempt with
    NOTHING to summarize is a skip that still consumes the window (the
    file would be inventory-only; the door will not email an empty
    spreadsheet) - next_due advances on every due attempt, whatever the
    outcome, so a broken endpoint cannot turn into a per-tick retry
    storm."""
    from .. import channel_endpoints as cep_svc
    from .. import system_events as events_svc

    recips = parse_report_recipients(row.to or "")
    scope = parse_report_chains(getattr(row, "chains", None) or "",
                                ceiling=None)
    if not scope and (row.chain or "").strip():
        scope = [row.chain.strip()]  # pre-v101 rows, defensively
    cadence_name = (row.cadence_seconds and next(
        (name for name, secs in CHAIN_REPORT_CADENCES.items()
         if secs == int(row.cadence_seconds)), "")) or ""
    csv_out = await chain_history_csv(
        db, row.owner_id, history_limit=row.history_limit,
        system=row.system or None, chains=scope or None)
    legs, rides = csv_out["leg_count"], csv_out["ride_count"]
    by_system = csv_out.get("by_system") or {}
    by_chain = csv_out.get("by_chain") or {}
    stamp = now.strftime("%Y%m%d")
    if len(scope) > 1:
        slug = "".join(
            c if c.isalnum() else "-" for c in
            "-".join(scope).lower())[:40].strip("-")
    elif scope:
        slug = "".join(c if c.isalnum() else "-"
                       for c in scope[0].lower())[:40].strip("-")
    elif row.system:
        slug = "sys-" + "".join(
            c if c.isalnum() else "-" for c in row.system.lower())[:36].strip("-")
    else:
        slug = ""
    slug = f"-{slug}" if slug else ""
    filename = f"py8n-chain-history{slug}-{stamp}.csv"

    result = {"at": now.isoformat(), "legs": legs, "rides": rides,
              "filename": filename, "to": ", ".join(recips),
              "recipients": len(recips), "chains": scope,
              "cadence": cadence_name}
    if not recips:
        result.update({"delivery": "skipped",
                       "detail": "the schedule names no recipient (to) - "
                                 "the file stayed home"})
    elif rides == 0:
        result.update({"delivery": "skipped",
                       "detail": "nothing to summarize - no ride in the "
                                 "window (the file would be inventory-only)"})
    else:
        endpoint = await escalations_svc.resolve_endpoint(db, row.owner_id,
                                                          "email")
        if endpoint is None:
            result.update({"delivery": "skipped",
                           "detail": "no email endpoint bound - bind one on "
                                     "/channels and the file crosses the wire"})
        else:
            subject = chain_report_subject(legs, rides, row.chain or "",
                                           row.system or "", chains=scope,
                                           cadence=cadence_name)
            def _buckets(b: dict[str, int]) -> str:
                return ", ".join(f"{name} {count} ride(s)"
                                 for name, count in sorted(
                                     b.items(), key=lambda kv: -kv[1]))
            sys_breakdown = _buckets(by_system)
            chain_breakdown = _buckets(by_chain)
            body = (
                f"The chain history, as the map draws it.\n\n"
                f"Window: every leg, the {row.history_limit} most recent "
                f"ride(s) each\n"
                + (f"Chains: {', '.join(scope)}\n" if len(scope) > 1 else "")
                + (f"Chain: {scope[0]}\n" if len(scope) == 1 else "")
                + (f"System: {row.system}\n" if row.system else "")
                + (f"Rhythm: weekly digest - the same envelope that carries "
                   f"the report, to the report's own list\n"
                   if cadence_name == "weekly" else "")
                + f"Shape: {legs} leg(s), {rides} ride(s) in the file\n"
                + (f"By chain: {chain_breakdown}\n" if chain_breakdown else "")
                + (f"By system: {sys_breakdown}\n" if sys_breakdown else "")
                + f"Recipients: {len(recips)}\n"
                f"Attachment: {filename}\n\n"
                f"Download the file any time from the chain views "
                f"(Export CSV) - this copy is the schedule's own.")
            try:
                delivery = await cep_svc.deliver_outbound(
                    endpoint, ", ".join(recips), body, subject=subject,
                    attachments=[{"filename": filename, "content": csv_out["csv"],
                                  "maintype": "text", "subtype": "csv"}])
                result.update({"delivery": delivery.get("delivery", "failed"),
                               "detail": delivery.get("detail", ""),
                               "subject": subject})
            except Exception as exc:  # noqa: BLE001 - an honest failure IS a result
                result.update({"delivery": "failed",
                               "detail": f"the dispatch refused loud: {exc}",
                               "subject": subject})
    row.last_sent_at = now
    row.next_due = now + timedelta(seconds=int(row.cadence_seconds))
    row.last_result = result
    row.updated_at = now
    await events_svc.emit(
        db, row.owner_id, "business.chain_report_dispatched", source="business",
        actor="scheduler", target_type="chain_report", target_id=row.id,
        payload={"to": result.get("to"), "recipients": len(recips),
                 "delivery": result["delivery"],
                 "detail": result.get("detail", ""), "legs": legs,
                 "rides": rides, "filename": filename,
                 "chain": row.chain or "", "system": row.system or "",
                 "chains": scope, "cadence": cadence_name,
                 "history_limit": row.history_limit,
                 "sent_at": result["at"]})
    return result


async def dispatch_due_chain_reports(db: AsyncSession, owner_id: str | None,
                                     *, now: datetime | None = None) -> list[dict]:
    """Every enabled schedule whose window has elapsed - the door's sweep
    rides this after the escalation walk (same session, same commit, the
    same injectable clock the tests replay)."""
    now = _aware(now) or _now()
    q = (select(ChainReportSchedule)
         .where(ChainReportSchedule.enabled.is_(True),
                ChainReportSchedule.next_due.is_not(None)))
    if owner_id is not None:
        q = q.where(ChainReportSchedule.owner_id == owner_id)
    rows = (await db.execute(q)).scalars().all()
    out = []
    for row in rows:
        due = _aware(row.next_due)
        if due is None or now < due:
            continue
        out.append(await dispatch_chain_report(db, row, now=now))
    return out


async def send_chain_report_now(db: AsyncSession, owner_id: str | None) -> dict:
    """The manual door (v99): one dispatch NOW, whatever next_due says -
    a real send is a real send (last_sent_at/next_due/last_result all
    stamp). The same renderer the scheduled walk uses, zero drift."""
    q = select(ChainReportSchedule)
    if owner_id is not None:
        q = q.where(ChainReportSchedule.owner_id == owner_id)
    else:
        q = q.where(ChainReportSchedule.owner_id.is_(None))
    row = (await db.execute(q)).scalars().first()
    if row is None:
        raise ProcessError("no chain-report schedule to send - save one first")
    result = await dispatch_chain_report(db, row, now=_now())
    return {"result": result, "schedule": _chain_report_out(row)}


async def escalation_history_grid(db: AsyncSession, owner_id: str | None,
                             days: int = 14) -> dict:
    """v93: the cross-machine ESCALATION HISTORY - per machine, per day,
    what the door did (escalations), what landed in the summaries
    (digests) and what the humans took (acks). The heatmap's grid: the
    door's pressure ACROSS machines over the last N days, read straight
    off the transition log - derived, nothing stored twice."""
    days = max(1, min(int(days or 14), 60))
    now = _now()
    start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0,
                                                     second=0, microsecond=0)
    q = (select(BusinessProcessTransitionLog, BusinessProcess.name)
         .join(BusinessProcess,
               BusinessProcess.id == BusinessProcessTransitionLog.process_id)
         .where(BusinessProcessTransitionLog.transition.in_(
             ESCALATION_HISTORY_TRANSITIONS),
             BusinessProcessTransitionLog.created_at >= start))
    if owner_id is not None:
        q = q.where(BusinessProcess.owner_id.in_((owner_id, None)))
    rows = (await db.execute(q)).all()
    day_keys = [(start + timedelta(days=i)).date().isoformat()
                for i in range(days)]
    machines: dict[str, dict] = {}
    for log, name in rows:
        m = machines.setdefault(log.process_id, {
            "process_id": log.process_id, "name": name,
            "cells": {k: {"escalations": 0, "acks": 0, "digests": 0}
                      for k in day_keys},
            "totals": {"escalations": 0, "acks": 0, "digests": 0}})
        key = _aware(log.created_at).date().isoformat()
        cell = m["cells"].get(key)
        if cell is None:
            continue
        if log.transition in ("escalate", "escalated"):
            cell["escalations"] += 1
            m["totals"]["escalations"] += 1
        elif log.transition == ACK_TRANSITION:
            cell["acks"] += 1
            m["totals"]["acks"] += 1
        elif log.transition == DIGEST_TRANSITION:
            cell["digests"] += 1
            m["totals"]["digests"] += 1
    out = sorted(machines.values(),
                 key=lambda m: -(m["totals"]["escalations"]
                                 + m["totals"]["acks"] + m["totals"]["digests"]))
    return {"days": day_keys, "machines": out, "days_count": days}


async def escalation_day_detail(db: AsyncSession, process_id: str, day: str,
                                owner_id: str | None) -> dict:
    """v96: ONE heatmap cell, opened - the machine's DAY. Every escalation
    row the door wrote on this machine between the day's midnight and the
    next (its knocks/moves, the team's acks, the digest receipts), read
    straight off the transition log with the entity each row belongs to
    (ref/title/state via the instance join). The drill-down the
    /processes heatmap cell click serves: the grid says WHERE the door
    pressed; this says WHAT happened, row by row, in order."""
    p = await _load_process(db, process_id, owner_id)
    day_s = str(day or "").strip()
    try:
        day_start = datetime.fromisoformat(f"{day_s}T00:00:00+00:00")
    except ValueError:
        raise ProcessError(
            f"day {day_s!r} is not an ISO date (YYYY-MM-DD) - the heatmap "
            "cells name their day") from None
    day_end = day_start + timedelta(days=1)
    q = (select(BusinessProcessTransitionLog, BusinessProcessInstance)
         .join(BusinessProcessInstance,
               BusinessProcessInstance.id == BusinessProcessTransitionLog.instance_id)
         .where(BusinessProcessTransitionLog.process_id == p.id,
                BusinessProcessTransitionLog.transition.in_(
                    ESCALATION_HISTORY_TRANSITIONS),
                BusinessProcessTransitionLog.created_at >= day_start,
                BusinessProcessTransitionLog.created_at < day_end))
    rows = (await db.execute(q)).all()
    counts = {"escalations": 0, "acks": 0, "digests": 0}
    out: list[dict] = []
    for log, inst in rows:
        if log.transition == ACK_TRANSITION:
            kind = "ack"
            counts["acks"] += 1
        elif log.transition == DIGEST_TRANSITION:
            kind = "digest"
            counts["digests"] += 1
        else:
            kind = "escalation"
            counts["escalations"] += 1
        payload = log.payload if isinstance(log.payload, dict) else {}
        at = _aware(log.created_at)
        out.append({
            "at": at.isoformat() if at else None,
            "transition": log.transition, "kind": kind,
            "instance_id": log.instance_id,
            "ref": inst.ref if inst else "", "title": inst.title if inst else "",
            "state": inst.state if inst else "",
            "actor": log.actor, "note": (log.note or "")[:200],
            "attempt": payload.get("attempt"),
            "overdue_seconds": payload.get("overdue_seconds"),
            "snooze_until": payload.get("snooze_until"),
            "reschedule_at": payload.get("reschedule_at"),
        })
    out.sort(key=lambda r: r["at"] or "")
    return {"process_id": p.id, "name": p.name, "day": day_s,
            "rows": out, "counts": counts, "total": len(out)}

