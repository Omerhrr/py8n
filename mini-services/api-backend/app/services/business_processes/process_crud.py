"""Business processes: process_out, create_process, _load_process, list_processes, get_process, _policy_diff_keys, update_escalation_policy, _instance_counts.

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
from .definitions import _terminal_states, validate_definition

def process_out(p: BusinessProcess, *, instance_counts: dict | None = None) -> dict:
    d = p.definition or {}
    return {"id": p.id, "name": p.name, "description": p.description,
            "definition": d, "states": d.get("states", []),
            "initial": d.get("initial"),
            "transitions": d.get("transitions", []),
            "escalation_policy": d.get("escalation_policy"),
            "escalation_summary": escalations_svc.describe_policy(
                escalations_svc.policy_from_definition(d)),
            "journeys": d.get("journeys", []),
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


def _policy_diff_keys(before: dict | None, after: dict | None) -> list[str]:
    """v92: the keys this save MOVES. Both sides come out of
    validate_escalation_policy (canonical shape, same key set), so a key
    differs only when its value truly changes - a no-op save diffs empty,
    a knock->digest switch names the clock keys it re-anchored, and a
    removal names every key the machine gave back."""
    b = before or {}
    a = after or {}
    return [k for k in sorted(set(b) | set(a)) if b.get(k) != a.get(k)]


async def update_escalation_policy(db: AsyncSession, process_id: str, *,
                                   owner_id: str | None, policy: dict | None,
                                   actor: str = "") -> dict:
    """v91: the policy is EDITABLE after install - the escalation door
    reads it fresh from the definition at every sweep
    (policy_from_definition inside escalate_stuck), so the new rhythm -
    channel, target, cadence, cap, knock <-> digest - takes effect on the
    NEXT tick with no restart and no migration. The instances and their
    running episodes are untouched: an episode mid-flight continues under
    the new rules (the same deal the door always offered hand-edited
    definitions, now a first-class door with the same loud validation).

    policy=None removes the policy - the machine falls back to the v85
    semantics (one knock per state stint, event-only).

    v92: the save comes with a RECEIPT - ``policy_diff`` names the
    before policy, the after policy, and the exact keys that moved, so
    the board can show the operator what they just changed."""
    p = await _load_process(db, process_id, owner_id)
    # v92: read the OLD policy first - both sides of the diff go through
    # the same validator, so the comparison is canonical (mode filled,
    # defaults named) and a no-op save diffs empty
    before = escalations_svc.policy_from_definition(dict(p.definition or {}))
    try:
        clean = escalations_svc.validate_escalation_policy(policy)
    except escalations_svc.EscalationPolicyError as exc:
        raise ProcessError(str(exc)) from exc
    changed = _policy_diff_keys(before, clean)
    d = dict(p.definition or {})
    if clean:
        d["escalation_policy"] = clean
    else:
        d.pop("escalation_policy", None)
    # fresh dict - the JSON column is never mutated in place
    p.definition = d
    await db.flush()
    await db.refresh(p)
    from .. import system_events as events_svc

    await events_svc.emit(
        db, p.owner_id, "business.policy_updated", source="business",
        actor=(actor or "api")[:140], target_type="process", target_id=p.id,
        payload={"process_id": p.id, "process_name": p.name,
                 "escalation_policy": clean,
                 "changed": changed,
                 "escalation_summary": escalations_svc.describe_policy(
                     escalations_svc.policy_from_definition(d))})
    counts = await _instance_counts(db, p.id)
    out = process_out(p, instance_counts=counts)
    out["policy_diff"] = {"before": before, "after": clean, "changed": changed}
    return out


async def _instance_counts(db: AsyncSession, process_id: str) -> dict:
    rows = (await db.execute(
        select(BusinessProcessInstance.state, func.count(BusinessProcessInstance.id))
        .where(BusinessProcessInstance.process_id == process_id)
        .group_by(BusinessProcessInstance.state))).all()
    return {state: int(n) for state, n in rows}

