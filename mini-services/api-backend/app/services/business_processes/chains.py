"""Business processes: _stuck_state, _ack_summary, _resolve_leg_target, chain_map, _chain_csv_rows, chain_history_csv.

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

def _stuck_state(row: BusinessProcessInstance, definition: dict,
                 now: datetime) -> tuple[bool, int]:
    """(is_stuck, overdue_seconds) - the same derivation every view uses."""
    due = _aware(row.due_at)
    if row.ended_at is not None or due is None or now <= due:
        return False, 0
    terminal = _terminal_states(definition) if definition else set()
    if terminal and row.state in terminal:
        return False, 0
    overdue = round((now - due).total_seconds())
    return True, overdue


def _ack_summary(book: dict, now: datetime) -> dict | None:
    """The episode's ack receipt as the views show it (v89 loan included)."""
    acked = book.get("acked")
    if not isinstance(acked, dict):
        return None
    out = {"by": str(acked.get("by") or ""), "at": acked.get("at"),
           "note": str(acked.get("note") or "")}
    until = escalations_svc.parse_iso(acked.get("snooze_until"))
    if until is not None:
        out["snooze_until"] = until.isoformat()
        out["snooze_remaining_seconds"] = max(0, round(
            (until - now).total_seconds()))
    return out


def _resolve_leg_target(procs: list[BusinessProcess], want: str) -> BusinessProcess | None:
    """Resolve a journey's target among the owner's INSTALLED machines -
    the same resolution the fire uses (id first, then case-insensitive
    name, most recent wins). None = the partner operator is not installed;
    the leg is still reported (honestly pending), never hidden."""
    want = str(want or "").strip()
    if not want:
        return None
    for p in procs:
        if p.id == want:
            return p
    hits = [p for p in procs if (p.name or "").lower() == want.lower()]
    if not hits:
        return None
    hits.sort(key=lambda p: p.created_at or datetime.min, reverse=True)
    return hits[0]


async def chain_map(db: AsyncSession, owner_id: str | None, *,
                    history_limit: int = 5) -> dict:
    """v93: the cross-machine CHAIN view - the journey legs the installed
    machines thread, drawn as chains with LIVE counts and per-leg HISTORY.

    Derived entirely from what is installed: every definition's journeys
    become a leg (source machine --on_state--> target machine, resolved
    by the fire's own resolution so an uninstalled partner shows as an
    honest pending leg naming it, never hidden). Legs walk into chains
    from the heads (machines with no incoming leg), the shelf's three
    canonical threads keep their names (Revenue / Supply / Care), and
    anything else names itself after its head.

    Every node carries the machine's live operation (open, stuck, the
    systems that bind it); every leg carries how many items have ridden
    it (opened), how many are still moving (open_now), how many the door
    is watching (stuck), and the recent traversals - the HISTORY the
    operator-detail chain draws: ref, title, where the item is now,
    when the leg opened it, and what the escalation door knows about it
    (overdue? acknowledged? snoozing?).

    v96: history_limit stretches the traversal window BEYOND the default
    5 (clamped 1..50) - a leg that has ridden for months does not lose
    its older rides just because the first cut drew a short list."""
    try:
        limit = max(1, min(int(history_limit)
                           if history_limit is not None else 5, 50))
    except (TypeError, ValueError):
        limit = 5
    procs = (await db.execute(
        select(BusinessProcess).order_by(BusinessProcess.created_at.asc()))).scalars().all()
    if owner_id is not None:
        procs = [p for p in procs if p.owner_id in (owner_id, None)]
    if not procs:
        return {"chains": [], "nodes": {}}
    by_id = {p.id: p for p in procs}
    definitions = {p.id: (p.definition or {}) for p in procs}
    now = _now()

    # the nodes' live operation - every instance of every involved machine
    inst_rows = (await db.execute(
        select(BusinessProcessInstance)
        .where(BusinessProcessInstance.process_id.in_([p.id for p in procs]))
        .order_by(BusinessProcessInstance.created_at.desc()))).scalars().all()
    nodes: dict[str, dict] = {}
    for p in procs:
        nodes[p.id] = {"process_id": p.id, "name": p.name,
                       "open": 0, "stuck": 0, "systems": []}
    for r in inst_rows:
        node = nodes.get(r.process_id)
        if node is None or r.ended_at is not None:
            continue
        node["open"] += 1
        stuck, _ = _stuck_state(r, definitions.get(r.process_id) or {}, now)
        if stuck:
            node["stuck"] += 1
    from ...models import Py8nSystem, SystemComponent

    comp_rows = (await db.execute(
        select(SystemComponent, Py8nSystem.name)
        .join(Py8nSystem, Py8nSystem.id == SystemComponent.system_id)
        .where(SystemComponent.kind == "process",
               SystemComponent.ref_id.in_([p.id for p in procs])))).all()
    for comp, sys_name in comp_rows:
        node = nodes.get(comp.ref_id)
        if node is not None and sys_name and sys_name not in node["systems"]:
            node["systems"].append(sys_name)

    # the legs - every journey on every installed definition
    legs: list[dict] = []
    out_map: dict[str, list[dict]] = {}
    in_map: dict[str, list[dict]] = {}
    in_targets: set[str] = set()
    for p in procs:
        for j in (definitions[p.id].get("journeys") or []):
            spec = j.get("open") or {}
            target_name = str(spec.get("process") or "").strip()
            target = _resolve_leg_target(procs, target_name)
            leg = {"from_process_id": p.id, "from_name": p.name,
                   "on_state": str(j.get("on_state") or ""),
                   "to_process_id": target.id if target else None,
                   "to_name": target.name if target else target_name,
                   "resolved": target is not None,
                   "due_in_seconds": spec.get("due_in_seconds"),
                   # v100: the systems the firing machine binds - the
                   # report's breakdown-by-system reads THIS (a machine
                   # bound to two systems lands its rides in both buckets,
                   # the way a pivot over the file would count them)
                   "systems": list(nodes[p.id]["systems"]),
                   "opened": 0, "open_now": 0, "stuck": 0, "history": []}
            legs.append(leg)
            out_map.setdefault(p.id, []).append(leg)
            if target is not None:
                in_map.setdefault(target.id, []).append(leg)
                in_targets.add(target.id)

    # per-leg ride counts + history - the instances whose journey link
    # names THIS leg (from_process + from_state, the fire's own stamp);
    # the link lives on the TARGET machine's instance
    for r in inst_rows:
        link = (r.context or {}).get("journey")
        if not isinstance(link, dict):
            continue
        src_l = str(link.get("from_process") or "").lower()
        st = str(link.get("from_state") or "")
        for leg in in_map.get(r.process_id, []):
            if src_l != (leg["from_name"] or "").lower() or st != leg["on_state"]:
                continue
            leg["opened"] += 1
            if r.ended_at is None:
                leg["open_now"] += 1
            stuck, overdue = _stuck_state(r, definitions.get(r.process_id) or {}, now)
            if stuck:
                leg["stuck"] += 1
            if len(leg["history"]) < limit:
                book = escalations_svc.episode_book(r)
                ack = _ack_summary(book, now)
                leg["history"].append({
                    "instance_id": r.id, "process_id": r.process_id,
                    "ref": r.ref, "title": r.title, "state": r.state,
                    "opened_at": link.get("at"),
                    "is_stuck": stuck,
                    "overdue_seconds": overdue,
                    "acked_by": (ack or {}).get("by"),
                    "snooze_remaining_seconds":
                        (ack or {}).get("snooze_remaining_seconds"),
                    "due_at": r.due_at.isoformat() if r.due_at else None})
    for leg in legs:
        leg["history"].sort(key=lambda h: h.get("opened_at") or "", reverse=True)
        leg["history_limit"] = limit
        leg["history_truncated"] = leg["opened"] > len(leg["history"])

    # the chains - walk from the heads (machines no resolved leg arrives at)
    heads = [p.id for p in procs if p.id not in in_targets and p.id in out_map]
    chains: list[dict] = []
    for head in heads:
        walk: list[dict] = []
        seen = {head}
        current = head
        while current in out_map:
            nxt_legs = out_map[current]
            if not nxt_legs:
                break
            leg = nxt_legs[0]
            walk.append(leg)
            if leg["to_process_id"] is None or leg["to_process_id"] in seen:
                break
            seen.add(leg["to_process_id"])
            current = leg["to_process_id"]
        if not walk:
            continue
        head_proc = by_id[head]
        name = CHAIN_NAMES.get((head_proc.name, walk[0]["on_state"])) \
            or f"Chain via {head_proc.name}"
        chains.append({"name": name, "head": head,
                       "head_name": head_proc.name, "legs": walk})
        for leg in walk:
            leg["chain"] = name
    # v98: a machine with MORE than one journey fires more legs than one
    # chain walk can carry (the walk rides the first out-leg per machine) -
    # the leftovers are honest legs with real history, so they ship as a
    # "side legs" group instead of vanishing; the CSV export reads them.
    side = [leg for leg in legs if "chain" not in leg]
    if side:
        chains.append({"name": SIDE_LEGS_CHAIN, "head": None,
                       "head_name": "", "legs": side})
        for leg in side:
            leg["chain"] = SIDE_LEGS_CHAIN
    return {"chains": chains, "nodes": nodes}


def _chain_csv_rows(chains: list[dict]) -> tuple[list[list], int, dict]:
    """v98: the per-leg chain history as CSV rows - one row per traversal,
    the leg (and its live counts) named on every row so a spreadsheet can
    filter or pivot per leg; a leg with no rides yet still ships one row
    with the ride columns empty (the leg inventory is in the file, the
    absence reads as data).

    A leg two walks both carry (two machines firing into the same target -
    a diamond the map honestly draws twice) is written ONCE, under the
    first chain that reached it: a pivot over this file must never count
    the same ride twice.

    v100: every row names the systems its firing machine binds (the
    "system" column, "|"-joined) and the ride count is broken down BY
    system - {system_name: rides} with a ride landing in every bucket its
    machine binds (a pivot's own arithmetic); machines bound to no system
    ride outside the breakdown, exactly as the file shows.

    v101: the rides break down BY CHAIN the same way ({chain_name: rides}
    - the first column IS the chain, so the pivot reads the file's own
    grouping); the weekly digest's body summarizes from it."""
    rows = [list(CHAIN_CSV_HEADER)]
    rides = 0
    by_system: dict[str, int] = {}
    by_chain: dict[str, int] = {}
    seen_legs: set[tuple[str, str]] = set()
    for ch in chains:
        for leg in ch.get("legs") or []:
            leg_id = (str(leg.get("from_process_id") or ""),
                      str(leg.get("on_state") or ""))
            if leg_id in seen_legs:
                continue
            seen_legs.add(leg_id)
            sla = leg.get("due_in_seconds")
            sys_cell = "|".join(leg.get("systems") or [])
            base = [ch.get("name") or "", leg.get("from_name") or "",
                    leg.get("on_state") or "", leg.get("to_name") or "",
                    sla if sla is not None else "",
                    leg.get("opened") or 0, leg.get("open_now") or 0,
                    leg.get("stuck") or 0,
                    "yes" if leg.get("history_truncated") else "no"]
            hist = leg.get("history") or []
            if not hist:
                # the inventory row: the leg's identity, the ride columns
                # empty (the absence reads as data), the systems named
                rows.append(base + [""] * 11 + [sys_cell])
                continue
            for h in hist:
                rides += 1
                ch_name = str(ch.get("name") or "")
                if ch_name:
                    by_chain[ch_name] = by_chain.get(ch_name, 0) + 1
                for sname in leg.get("systems") or []:
                    by_system[sname] = by_system.get(sname, 0) + 1
                overdue = h.get("overdue_seconds")
                snooze = h.get("snooze_remaining_seconds")
                rows.append(base + [
                    h.get("ref") or "", h.get("title") or "",
                    h.get("state") or "", h.get("opened_at") or "",
                    h.get("due_at") or "",
                    "yes" if h.get("is_stuck") else "no",
                    overdue if overdue is not None else "",
                    h.get("acked_by") or "",
                    snooze if snooze is not None else "",
                    h.get("instance_id") or "", h.get("process_id") or ""
                ] + [sys_cell])
    return rows, rides, by_system, by_chain


async def chain_history_csv(db: AsyncSession, owner_id: str | None, *,
                            history_limit: int = 50,
                            chain: str | None = None,
                            leg: str | None = None,
                            system: str | None = None,
                            chains: list[str] | None = None) -> dict:
    """v98: the per-leg chain history as a CSV download - the same map
    the operator-detail chain draws (chain_map, zero drift), rendered as
    one row per traversal with the chain and the leg named on every row.
    history_limit rides the same clamp the map uses (1..50, default the
    deepest), so the export honors exactly the window the toggle chose.

    Columns: chain, leg identity (from / on_state / to / sla seconds),
    the leg's live counts (opened / open_now / stuck, history_truncated),
    then the ride itself (ref, title, state, opened_at, due_at, is_stuck,
    overdue_seconds, acked_by, snooze_remaining_seconds) and the row's
    own ids, then the systems the firing machine binds (v100, the
    breakdown dimension). Returns {csv, leg_count, ride_count, by_system,
    chain_filter, leg_filter, system_filter}.

    v99: the PLOT's own filters ride the SAME map - ``chain`` names one
    chain (case-insensitive; the per-chain CSV button), ``leg`` names
    one leg as "from_name|on_state" (case-insensitive; the per-leg CSV
    chip). v100: ``system`` names one SYSTEM (case-insensitive; the
    systems page's per-system export and the report's own scope) - a leg
    rides in when the machine FIRING it binds that system; a machine
    bound to no system never matches a named filter. All three compose;
    an unknown name is an HONEST EMPTY file (header only, leg_count 0) -
    the absence reads as data, never a 404, because the map the operator
    is looking at is the truth.

    v101: ``chains`` names a LIST of chains - the report's own tag list
    (case-insensitive, composing with everything else; the wire param is
    comma-separated, the schedule stores it parsed). One want-set against
    the map: a chain rides in when its name is on the list. The output
    grows by_chain (the ride buckets per chain - the digest body's own
    breakdown) and the chains_filter echo (the REQUESTED names, the way
    chain_filter echoes what was asked, not what matched)."""
    out = await chain_map(db, owner_id, history_limit=history_limit)
    requested_chains = [str(c).strip() for c in (chains or [])
                        if str(c).strip()]
    all_chains = out.get("chains") or []
    want_chain: set[str] = set()
    if chain is not None and str(chain).strip():
        want_chain.add(str(chain).strip().lower())
    for cname in requested_chains:
        want_chain.add(cname.lower())
    if want_chain:
        all_chains = [c for c in all_chains
                      if (c.get("name") or "").strip().lower() in want_chain]
    chains = all_chains
    if leg is not None and str(leg).strip():
        raw = str(leg).strip().lower()
        if "|" in raw:
            want_from, want_state = (p.strip() for p in raw.split("|", 1))
        else:
            want_from, want_state = raw, ""
        filtered: list[dict] = []
        for c in chains:
            legs = [l for l in (c.get("legs") or [])
                    if (l.get("from_name") or "").strip().lower() == want_from
                    and (l.get("on_state") or "").strip().lower() == want_state]
            if legs:
                filtered.append({**c, "legs": legs})
        chains = filtered
    if system is not None and str(system).strip():
        want_sys = str(system).strip().lower()
        filtered = []
        for c in chains:
            legs = [l for l in (c.get("legs") or [])
                    if any((s or "").strip().lower() == want_sys
                           for s in (l.get("systems") or []))]
            if legs:
                filtered.append({**c, "legs": legs})
        chains = filtered
    rows, rides, by_system, by_chain = _chain_csv_rows(chains)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerows(rows)
    leg_count = sum(len(c.get("legs") or []) for c in chains)
    return {"csv": buf.getvalue(), "leg_count": leg_count,
            "ride_count": rides, "by_system": by_system,
            "by_chain": by_chain,
            "chain_filter": (str(chain).strip() if chain else ""),
            "leg_filter": (str(leg).strip() if leg else ""),
            "system_filter": (str(system).strip() if system else ""),
            "chains_filter": ", ".join(requested_chains)}

