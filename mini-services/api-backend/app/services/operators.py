"""Marketplace operators (v83 + v86) - "Install a business operator".

A solution ships a PACK (workflows + datasets, installed inactive). An
operator ships the BUSINESS: the interaction primitives (AI agent,
meeting room, waiting queue, outbound campaign), the REACTIVE workflows
wired to the v80 event system (event_trigger -> shape -> dataset write),
the datasets the business writes into, and the STAFF DASHBOARD over
those datasets - all bound into a RUNNING Py8nSystem (v81) with the
operations log and the system.installed event.

"Install a business operator" not "Install a workflow template": one
click hires a department that is already wired - meetings that log
themselves when they end, leads that score themselves when calls end,
appointment requests that land from inbound texts. The credentials
(SMS channels, dialing endpoints, LLM brains) are the installer's to
bind; until they exist the passes record honest skips - never silent
ones. Channels stay interchangeable infrastructure: the operator owns
the system underneath them.

v86 grew the shelf to NINE and closed the intake loop: every
department-shaped operator (support, operations, hr, finance,
procurement, logistics) now ships a pre-wired PROCESS seeded from its
dataset, an intake workflow that opens tracked instances (the v86
business_start node), an advancer that moves them on real calls, and
escalate self-loops for the scheduler door - the machine watches every
entity from the moment it exists.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..engine.runner import validate_graph_document
from .operators_catalog import CATALOG as _CATALOG


class OperatorError(ValueError):
    """Honest operator install failures."""


# ---------------------------------------------------------------------------
# The curated shelf - every operator declares the exact topology it builds.
# The nine operator dicts themselves live in operators_catalog/ (task #3
# split: nine ~150-200 line declarative dicts back to back dominated this
# file's length) - _CATALOG above preserves their original shelf order.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# v87: the escalation POLICY per operator machine - the door's channel +
# repeat dimension. The machines above define the escalate move (the door
# takes it); these policies say WHO GETS TOLD (the channel, through the
# owner's channel endpoints - provider-agnostic) and HOW OFTEN the door
# knocks again (repeat_every_seconds, until 1 + max_repeats attempts; a
# state change starts a fresh episode). `to` stays empty on the shelf: the
# installer binds the target, the passes skip honestly until then.
# ---------------------------------------------------------------------------
_ESCALATION_POLICIES: dict[str, dict] = {
    "Lead pipeline":       {"channel": "email", "to": "", "repeat_every_seconds": 3600,  "max_repeats": 3},
    "Meeting lifecycle":   {"channel": "email", "to": "", "repeat_every_seconds": 1800,  "max_repeats": 2,
                            "message_template": ("[Meeting Operator] '{title}' (ref {ref}) is still "
                                                 "'{state}' after {overdue_minutes} minutes - please "
                                                 "confirm or cancel it (escalation {attempt}).")},
    "Appointment journey": {"channel": "sms",   "to": "", "repeat_every_seconds": 900,   "max_repeats": 3,
                            "message_template": ("[Clinic Operator] Appointment {ref} ('{title}') is "
                                                 "still '{state}' after {overdue_minutes} minutes - "
                                                 "confirm or call the patient (escalation {attempt}).")},
    "Case lifecycle":      {"channel": "email", "to": "", "repeat_every_seconds": 14400, "max_repeats": 3},
    "Request lifecycle":   {"channel": "email", "to": "", "repeat_every_seconds": 14400, "max_repeats": 3},
    "Leave pipeline":      {"channel": "email", "to": "", "repeat_every_seconds": 14400, "max_repeats": 3},
    "Onboarding pipeline": {"channel": "email", "to": "", "repeat_every_seconds": 86400, "max_repeats": 2},
    # v89: the invoice chaser is the classic digest case - a DAILY summary
    # of everything past due instead of four knocks per invoice per day
    "Invoice lifecycle":   {"channel": "email", "to": "", "mode": "digest",
                            "digest_every_seconds": 86400, "max_repeats": 3},
    "Purchase lifecycle":  {"channel": "email", "to": "", "repeat_every_seconds": 43200, "max_repeats": 3},
    "Delivery pipeline":   {"channel": "sms",   "to": "", "repeat_every_seconds": 21600, "max_repeats": 3},
    "Customer onboarding": {"channel": "email", "to": "", "repeat_every_seconds": 43200, "max_repeats": 3},
}


# v89/v90: cross-operator JOURNEYS per operator machine - when the machine
# lands on the fire-state, the next leg OPENS ITSELF on the target machine
# (a won deal opening an onboarding case). The target is named here and
# resolved at FIRE time by name - operators install independently, so an
# uninstalled target skips honestly (the event names it; install the
# second operator and the handoff becomes real). v90 threads the legs into
# CHAINS that span three departments, each opened leg carrying its own SLA
# promise (due_in_seconds) so the door and its digests watch it from birth:
#
#   the REVENUE chain:  Sales (Lead pipeline) --won--> Operations (Customer
#     onboarding) --handed_off--> Finance (Invoice lifecycle)
#   the SUPPLY chain:   Procurement (Purchase lifecycle) --ordered-->
#     Logistics (Delivery pipeline) --delivered--> Finance (Invoice)
#   the CARE chain:     Clinic (Appointment journey) --billed--> Finance
#
# one ref (the phone the business already speaks in) rides every leg, so
# the whole journey is greppable across departments.
_JOURNEYS: dict[str, list[dict]] = {
    "Lead pipeline": [
        {"on_state": "won",
         "open": {"process": "Customer onboarding",
                  "title_template": "Onboarding - {title}",
                  "memory": {"via": "won-deal journey",
                             "source_operator": "sales"}}},
    ],
    "Customer onboarding": [
        {"on_state": "handed_off",
         "open": {"process": "Invoice lifecycle",
                  "title_template": "Billing - {title}",
                  "ref_template": "{ref}",
                  "memory": {"via": "handed-off journey",
                             "source_operator": "operations"},
                  "due_in_seconds": 5 * 24 * 3600}},
    ],
    "Purchase lifecycle": [
        {"on_state": "ordered",
         "open": {"process": "Delivery pipeline",
                  "title_template": "Delivery - {title}",
                  "ref_template": "{ref}",
                  "memory": {"via": "ordered journey",
                             "source_operator": "procurement"},
                  "due_in_seconds": 2 * 24 * 3600}},
    ],
    "Delivery pipeline": [
        {"on_state": "delivered",
         "open": {"process": "Invoice lifecycle",
                  "title_template": "Bill - {title}",
                  "ref_template": "{ref}",
                  "memory": {"via": "delivered journey",
                             "source_operator": "logistics"},
                  "due_in_seconds": 5 * 24 * 3600}},
    ],
    "Appointment journey": [
        {"on_state": "billed",
         "open": {"process": "Invoice lifecycle",
                  "title_template": "Visit - {title}",
                  "ref_template": "{ref}",
                  "memory": {"via": "billed visit journey",
                             "source_operator": "clinic"},
                  "due_in_seconds": 5 * 24 * 3600}},
    ],
}


def _journeys_for(pspec: dict) -> list[dict] | None:
    """The shelf journeys for a pack's process, applied into the
    definition at install time (validate_definition re-validates them
    loudly)."""
    name = str(pspec.get("name") or "").strip()
    journeys = _JOURNEYS.get(name)
    return [dict(j, open=dict(j["open"])) for j in journeys] if journeys else None


def _policy_for(pspec: dict) -> dict | None:
    """The shelf policy for a pack's process, applied into the definition
    at install time (validate_definition re-validates it loudly)."""
    name = str(pspec.get("name") or "").strip()
    policy = _ESCALATION_POLICIES.get(name)
    if policy is None:
        # any machine the table does not name still gets a sane default:
        # event-only, hourly, three repeats
        return {"channel": "", "to": "", "repeat_every_seconds": 3600,
                "max_repeats": 3}
    return dict(policy)




def _describe_pack_policy(pspec: dict) -> str:
    """The shelf's one-line escalation summary for a pack's process."""
    from . import escalations as escalations_svc

    return escalations_svc.describe_policy(_policy_for(pspec))


def _describe_pack_journeys(pspec: dict) -> list[dict]:
    """The shelf's journey display for a pack's process."""
    journeys = _journeys_for(pspec) or []
    return [{"on_state": j["on_state"], "opens": j["open"]["process"]}
            for j in journeys]


def _onboarding_loop(pspec: dict) -> dict | None:
    """v88: the department's DATA on-ramp - one workflow per machine,
    generated from the pack's own seed spec. The channel intakes
    (sms.received, call.ended) onboard the entities that ARRIVE through a
    channel; this loop onboards the entities that arrive as DATA: the
    spreadsheet that just landed, the rows the staff typed in the App
    Builder, the bulk import appended over the API. A dataset-trigger
    watches the machine's own intake dataset; every new version wakes the
    business_onboard step, which starts one tracked instance PER ROW at
    the row's own stage - idempotently (an open instance already carrying
    the ref skips, so the install-time seeding and the loop never fight).
    """
    ds = str(pspec.get("seed_from_dataset") or "").strip()
    if not ds:
        return None
    machine = str(pspec.get("name") or "").strip()
    due_s = pspec.get("due_in_seconds")
    return {
        "name": f"{machine} onboarding",
        "description": (
            f"The data on-ramp: new rows landing in {ds!r} onboard as tracked "
            f"instances of {machine!r} at each row's own stage - idempotent "
            "(already-tracked refs skip), so re-runs and trigger fires never "
            "double-track. Fires on dataset versions; run it by hand after a "
            "bulk import too."),
        "trigger": {"type": "dataset_trigger",
                    "params": {"dataset": ds, "poll_seconds": 60}},
        "steps": [
            {"type": "business_onboard",
             "name": f"Onboard rows into {machine}",
             "params": {"process": machine,
                        "dataset": ds,
                        "ref_column": str(pspec.get("ref_column") or ""),
                        "state_column": str(pspec.get("state_column") or ""),
                        "title_columns": [str(c) for c in (pspec.get("title_from") or [])],
                        "due_in_seconds": int(due_s) if due_s else None,
                        "on_duplicate": "skip",
                        "actor": "onboarding-loop"}},
        ],
    }


OPERATORS: list[dict] = list(_CATALOG)
OPERATORS_BY_SLUG = {op["slug"]: op for op in OPERATORS}

# v88: BROADENING THE DEPARTMENT ONBOARDING LOOPS - every machine gets its
# data on-ramp beside the channel intakes (generated, never hand-copied:
# the loop is built from the same seed spec the install seeds with, so
# the two doors can never drift apart)
for _op in OPERATORS:
    for _pspec in (_op.get("processes") or []):
        _loop = _onboarding_loop(_pspec)
        if _loop is not None:
            _op["workflows"].append(_loop)
del _op, _pspec, _loop


# v92: the NAMED chains - the drawn view on the operator detail page. A
# chain names its ordered walk as (source process, fire state) pairs;
# everything else (the opened process, the leg's own SLA) RESOLVES from
# _JOURNEYS so the two tables can never drift: a leg that is not a
# journey, or a walk that does not connect, refuses loudly at import.
_CHAINS: list[dict] = [
    {"slug": "revenue", "name": "Revenue",
     "story": "a deal won onboards the customer, and the hand-off lands the invoice",
     "path": [("Lead pipeline", "won"), ("Customer onboarding", "handed_off")]},
    {"slug": "supply", "name": "Supply",
     "story": "a purchase order dispatches the delivery, and the goods received land the bill",
     "path": [("Purchase lifecycle", "ordered"), ("Delivery pipeline", "delivered")]},
    {"slug": "care", "name": "Care",
     "story": "a billed visit hands the money to finance",
     "path": [("Appointment journey", "billed")]},
]


def _resolve_chains() -> list[dict]:
    """Resolve the chain walks against _JOURNEYS once at import - the
    drawn view renders RESOLVED legs, never re-derives them."""
    resolved: list[dict] = []
    for chain in _CHAINS:
        legs: list[dict] = []
        for i, (src, state) in enumerate(chain["path"]):
            hits = [j for j in _JOURNEYS.get(src, []) if j["on_state"] == state]
            if len(hits) != 1:
                raise RuntimeError(
                    f"chain {chain['slug']!r}: leg ({src!r} on {state!r}) is not "
                    "a journey in _JOURNEYS - the walk and the journeys drifted")
            open_spec = hits[0].get("open") or {}
            opens = str(open_spec.get("process") or "")
            nxt = chain["path"][i + 1][0] if i + 1 < len(chain["path"]) else None
            if nxt and opens != nxt:
                raise RuntimeError(
                    f"chain {chain['slug']!r}: leg ({src!r} on {state!r}) opens "
                    f"{opens!r}, but the walk continues at {nxt!r}")
            legs.append({"from_process": src, "on_state": state,
                         "opens": opens,
                         "due_in_seconds": open_spec.get("due_in_seconds")})
        resolved.append({**chain, "legs": legs})
    return resolved


_RESOLVED_CHAINS = _resolve_chains()


def _journey_owner_of() -> dict[str, str]:
    owner_of: dict[str, str] = {}
    for op in OPERATORS:
        for p in (op.get("processes") or []):
            owner_of[p["name"]] = op["slug"]
    return owner_of


def _operator_brief(slug: str) -> dict:
    op = OPERATORS_BY_SLUG.get(slug)
    if op is None:
        return {"slug": slug, "name": slug, "icon": "", "color": "#71717a"}
    return {"slug": op["slug"], "name": op["name"],
            "icon": op["icon"], "color": op["color"]}


def _chains_for(slug: str) -> list[dict]:
    """v92: the named chains THIS operator sits in - drawn on the detail
    page. ``position`` is where the operator's node sits in the walk
    (0 = the chain starts here, the last index = the terminus), and each
    leg names who FIRES it and who RECEIVES, so the drawing can flag the
    operator's own legs (out = it fires, in = it is fed)."""
    owner_of = _journey_owner_of()
    out: list[dict] = []
    for chain in _RESOLVED_CHAINS:
        walk = [chain["legs"][0]["from_process"]] + \
               [leg["opens"] for leg in chain["legs"]]
        ops_order: list[str] = []
        for pr in walk:
            s = owner_of.get(pr, "")
            if s and s not in ops_order:
                ops_order.append(s)
        if slug not in ops_order:
            continue
        legs = [{**leg,
                 "from_operator": owner_of.get(leg["from_process"], ""),
                 "opens_operator": owner_of.get(leg["opens"], "")}
                for leg in chain["legs"]]
        out.append({"slug": chain["slug"], "name": chain["name"],
                    "story": chain["story"],
                    "operators": [_operator_brief(s) for s in ops_order],
                    "legs": legs,
                    "position": ops_order.index(slug)})
    return out


def _catalog_journeys(slug: str) -> list[dict]:
    """v91: the journey legs ON the shelf card - what installing this
    operator wires into the cross-department chains. Two directions,
    both honest: 'out' = this operator's machine lands on a state and the
    next department's leg opens ITSELF; 'in' = another operator's machine
    hands work TO this one (the leg completes when BOTH are installed -
    targets resolve by name at fire time, so a missing operator is an
    honest skip, never a broken install)."""
    own = {p["name"] for p in (OPERATORS_BY_SLUG[slug].get("processes") or [])}
    owner_of: dict[str, str] = {}
    for op in OPERATORS:
        for p in (op.get("processes") or []):
            owner_of[p["name"]] = op["slug"]
    legs: list[dict] = []
    for src_name, js in _JOURNEYS.items():
        for j in js:
            open_spec = j.get("open") or {}
            target = str(open_spec.get("process") or "")
            if src_name in own:
                legs.append({"direction": "out", "from_process": src_name,
                             "from_operator": owner_of.get(src_name, ""),
                             "on_state": j["on_state"], "opens": target,
                             "due_in_seconds": open_spec.get("due_in_seconds")})
            elif target in own:
                legs.append({"direction": "in", "from_process": src_name,
                             "from_operator": owner_of.get(src_name, ""),
                             "on_state": j["on_state"], "opens": target,
                             "due_in_seconds": open_spec.get("due_in_seconds")})
    return legs


def operator_catalog() -> dict:
    """The operators shelf - what each install BUILDS, counted honestly."""
    return {
        "operators": [
            {"slug": op["slug"], "name": op["name"], "tagline": op["tagline"],
             "category": op["category"], "icon": op["icon"], "color": op["color"],
             "outcomes": list(op["outcomes"]),
             "journeys": _catalog_journeys(op["slug"]),
             "chains": [c["slug"] for c in _chains_for(op["slug"])],
             "topology": {
                 "datasets": len(op["datasets"]),
                 "workflows": len(op["workflows"]),
                 "agents": 1 if op.get("agent") else 0,
                 "rooms": len(op["rooms"]),
                 "queues": len(op["queues"]),
                 "campaign": 1 if op.get("campaign") else 0,
                 "processes": len(op.get("processes") or []),
                 "dashboard": 1,
             }}
            for op in OPERATORS
        ]
    }


def operator_detail(slug: str) -> dict:
    op = OPERATORS_BY_SLUG.get(slug)
    if op is None:
        raise OperatorError(f"unknown operator {slug!r}")
    return {
        "slug": op["slug"], "name": op["name"], "tagline": op["tagline"],
        "category": op["category"], "icon": op["icon"], "color": op["color"],
        "outcomes": list(op["outcomes"]),
        "chains": _chains_for(slug),
        "installs": {
            "datasets": [{"name": d["name"], "description": d["description"],
                          "columns": list(d["columns"]), "rows": len(d["rows"])}
                         for d in op["datasets"]],
            "workflows": [{"name": w["name"], "description": w["description"],
                           "trigger": (w["trigger"]["params"].get("event_type")
                                       or (f"dataset:{w['trigger']['params'].get('dataset')}"
                                           if w["trigger"]["type"] == "dataset_trigger" else ""))}
                          for w in op["workflows"]],
            "processes": [{"name": p["name"],
                           "states": p["definition"]["states"],
                           "seeded_from": p.get("seed_from_dataset"),
                           "escalates": any(t.get("name") == "escalate"
                                            for t in p["definition"]["transitions"]),
                           "escalation": _describe_pack_policy(p),
                           "journeys": _describe_pack_journeys(p)}
                          for p in (op.get("processes") or [])],
            "agent": {"name": op["agent"]["name"],
                      "knowledge": (op["agent"]["knowledge"] or {}).get("dataset")},
            "rooms": [{"name": r["name"], "modality": r["modality"]} for r in op["rooms"]],
            "queues": [q["name"] for q in op["queues"]],
            "campaign": (op["campaign"] or {}).get("name"),
            "dashboard": op["dashboard"]["name"],
        },
        "notes": list(op["notes"]),
    }


# ---------------------------------------------------------------------------
# v93: the chains LIVE on the installed system - the drawn walk with the
# real instance counts underneath it
# ---------------------------------------------------------------------------

async def chains_for_system(db: AsyncSession, system, *,
                            now: datetime | None = None) -> list[dict]:
    """The chains this system's bound processes actually sit in, with the
    LIVE counts per node and per leg - the operator detail page's drawn
    walk, re-rendered after the install against real rows.

    The chain walks resolve by PROCESS NAME against the system's bound
    kind="process" components (the same name-resolution the journeys
    themselves use at fire time), so a system that binds only part of a
    chain still shows it - honestly: a leg with a missing end is drawn
    dashed (``bound_from``/``bound_opens`` name who is absent) and the
    chain carries ``complete``. Counts are computed in PYTHON per the
    house SQLite naive/aware discipline:

    * per NODE (a bound process in the walk): open instances + how many
      are past their SLA (terminal states skipped - a closed entity does
      not count as late), plus v94's ack/snooze surfacing: ``acked`` /
      ``snoozed`` count the overdue ones the door is holding (the ack
      holds the DOOR, never the clock - the row stays overdue either
      way), and ``overdue_instances`` names them (most overdue first,
      capped at 5) with each one's escalation book - the same receipt the
      attention feed carries, so the chain node answers "who has this"
      without leaving the drawing;
    * per LEG: ``in_state`` = the source process's open instances sitting
      in the fire state right now (the hand-off is armed), ``fired`` =
      the target process's OPEN instances this leg opened itself (they
      carry the journey link in their context), ``overdue`` = the fired
      ones past the leg's own SLA promise - with the same ``acked`` /
      ``snoozed`` sub-counts among them.

    A system that binds no processes draws no chains - the section is
    simply absent, never an empty lie."""
    from datetime import datetime as _dt, timezone as _tz

    from ..models import BusinessProcess, BusinessProcessInstance
    from . import business_processes as process_svc
    from . import escalations as escalations_svc  # v94: the ack book reader

    if now is None:
        now = _dt.now(_tz.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=_tz.utc)  # SQLite returns naive - normalize
    ref_ids = [c.ref_id for c in (system.components or [])
               if c.kind == "process"]
    if not ref_ids:
        return []
    procs = (await db.execute(
        select(BusinessProcess).where(BusinessProcess.id.in_(ref_ids)))).scalars().all()
    if not procs:
        return []
    bound: dict[str, BusinessProcess] = {p.name: p for p in procs}
    terminal_of: dict[str, set[str]] = {
        p.name: process_svc._terminal_states(p.definition or {}) for p in procs}

    open_rows = (await db.execute(
        select(BusinessProcessInstance)
        .where(BusinessProcessInstance.process_id.in_([p.id for p in procs]),
               BusinessProcessInstance.ended_at.is_(None)))).scalars().all()

    def _late(row) -> bool:
        due = row.due_at
        if due is None:
            return False
        if due.tzinfo is None:
            due = due.replace(tzinfo=_tz.utc)
        return now >= due

    # v94: the ack/snooze surfacing - read from the episode book the door
    # keeps on the instance's memory (the SAME receipt the attention feed
    # carries). The ack holds the door, never the clock: an acked row is
    # still overdue, so acked/snoozed are sub-counts of overdue. A snooze
    # is a LOAN - ``snooze_active`` is False once snooze_until has passed
    # (the door re-knocks; the ack itself still shows).
    def _book_of(row) -> dict:
        book = process_svc.escalations_svc.episode_book(row)
        acked = book.get("acked") if isinstance(book.get("acked"), dict) else None
        snooze_until = str((acked or {}).get("snooze_until") or "")
        snooze_active = False
        if snooze_until:
            until = escalations_svc._parse_iso(snooze_until)
            snooze_active = until is not None and now < until
        return {
            "count": int(book.get("count") or 0),
            "last_delivery": str(book.get("last_delivery") or ""),
            "acked_by": str((acked or {}).get("by") or ""),
            "snooze_until": snooze_until,
            "snooze_active": snooze_active,
        }

    def _ack_counts(rows: list) -> tuple[int, int]:
        acked = snoozed = 0
        for r in rows:
            b = _book_of(r)
            if b["acked_by"]:
                acked += 1
            if b["snooze_active"]:
                snoozed += 1
        return acked, snoozed

    nodes_stat: dict[str, dict] = {}
    for name, proc in bound.items():
        rows = [r for r in open_rows if r.process_id == proc.id]
        term = terminal_of.get(name) or set()
        late_rows = [r for r in rows
                     if r.due_at is not None and r.state not in term and _late(r)]
        # most overdue first, capped - the node names who needs the human
        # without turning the drawing into a feed
        late_rows.sort(
            key=lambda r: (now - (r.due_at.replace(tzinfo=_tz.utc)
                                  if r.due_at.tzinfo is None else r.due_at)
                           ).total_seconds(),
            reverse=True)
        acked, snoozed = _ack_counts(late_rows)
        nodes_stat[name] = {
            "open": len(rows),
            "overdue": len(late_rows),
            "acked": acked,
            "snoozed": snoozed,
            "overdue_instances": [{
                "process_id": proc.id, "instance_id": r.id,
                "ref": r.ref, "title": r.title, "state": r.state,
                "overdue_seconds": round((now - (
                    r.due_at.replace(tzinfo=_tz.utc)
                    if r.due_at.tzinfo is None else r.due_at)).total_seconds()),
                "escalation": _book_of(r),
            } for r in late_rows[:5]],
        }

    # the legs' fired children: open instances carrying the journey link
    fired_index: dict[tuple[str, str], list] = {}
    for r in open_rows:
        link = (r.context or {}).get("journey")
        if isinstance(link, dict) and link.get("from_process"):
            key = (str(link["from_process"]), str(link.get("from_state") or ""))
            fired_index.setdefault(key, []).append(r)

    owner_of = _journey_owner_of()
    out: list[dict] = []
    for chain in _RESOLVED_CHAINS:
        legs = chain["legs"]
        walk = [legs[0]["from_process"]] + [leg["opens"] for leg in legs]
        if not any(pr in bound for pr in walk):
            continue  # none of this chain lives on this system
        leg_out: list[dict] = []
        for leg in legs:
            src, dst = leg["from_process"], leg["opens"]
            bound_from, bound_opens = src in bound, dst in bound
            in_state = 0
            if bound_from:
                in_state = len([r for r in open_rows
                                if r.process_id == bound[src].id
                                and r.state == leg["on_state"]])
            children = [r for r in fired_index.get((src, leg["on_state"]), [])
                        if bound_opens and r.process_id == bound[dst].id]
            late_children = [r for r in children if _late(r)]
            child_acked, child_snoozed = _ack_counts(late_children)
            leg_out.append({
                "from_process": src, "on_state": leg["on_state"],
                "opens": dst,
                "due_in_seconds": leg.get("due_in_seconds"),
                "from_operator": owner_of.get(src, ""),
                "opens_operator": owner_of.get(dst, ""),
                "bound_from": bound_from, "bound_opens": bound_opens,
                "counts": {"in_state": in_state, "fired": len(children),
                           "overdue": len(late_children),
                           "acked": child_acked, "snoozed": child_snoozed},
            })
        ops_order: list[str] = []
        for pr in walk:
            s = owner_of.get(pr, "")
            if s and s not in ops_order:
                ops_order.append(s)
        out.append({
            "slug": chain["slug"], "name": chain["name"], "story": chain["story"],
            "operators": [_operator_brief(s) for s in ops_order],
            "legs": leg_out,
            "nodes": [{"process": pr, "operator": owner_of.get(pr, ""),
                       "bound": pr in bound,
                       **nodes_stat.get(pr, {"open": 0, "overdue": 0})}
                      for pr in walk],
            "complete": all(l["bound_from"] and l["bound_opens"] for l in leg_out),
        })
    return out


# ---------------------------------------------------------------------------
# The install - compose the business into real primitives, bind it RUNNING
# ---------------------------------------------------------------------------

def _node(nid: str, ntype: str, params: dict, name: str) -> dict:
    return {"id": nid, "type": ntype, "name": name,
            "position": {"x": 0, "y": 0}, "parameters": params}


def _edge(eid: str, source: str, target: str) -> dict:
    return {"id": eid, "source": source, "target": target,
            "sourceHandle": "main", "targetHandle": "main"}


def _workflow_graph(wspec: dict) -> dict:
    """trigger + linear step chain - a REAL graph of registered node types."""
    nodes: list[dict] = []
    edges: list[dict] = []
    trig = wspec.get("trigger") or {}
    nodes.append(_node("n_trigger", str(trig.get("type") or "event_trigger"),
                       dict(trig.get("params") or {}), "Trigger"))
    prev = "n_trigger"
    for i, s in enumerate(wspec.get("steps") or []):
        s = s if isinstance(s, dict) else {}
        nid = f"n_step_{i}"
        nodes.append(_node(nid, str(s.get("type")), dict(s.get("params") or {}),
                           str(s.get("name") or s.get("type") or f"Step {i + 1}")))
        edges.append(_edge(f"n_edge_{i}", prev, nid))
        prev = nid
    return {"nodes": nodes, "edges": edges}


async def _unique_dataset_name(db: AsyncSession, base: str) -> str:
    from . import datasets as ds_svc

    name = re.sub(r"\s+", " ", base).strip()[:100] or "Operator Dataset"
    if not ds_svc.NAME_RE.match(name):
        name = "Operator Dataset"
    candidate = name
    n = 1
    while await ds_svc.name_taken(db, candidate):
        n += 1
        candidate = f"{name} {n}"
    return candidate


async def _unique_dashboard_name(db: AsyncSession, base: str) -> str:
    from . import dashboards as dash_svc

    name = re.sub(r"\s+", " ", base).strip()[:140] or "Operator Board"
    candidate = name
    n = 1
    while await dash_svc.name_taken(db, candidate):
        n += 1
        candidate = f"{name} {n}"
    return candidate


async def install_operator(db: AsyncSession, slug: str, *, owner_id: str | None,
                           llm_credential_id: str | None = None,
                           brain: str = "scaffold", note: str = "") -> dict:
    """Compose the operator's business into real primitives and bind it into
    a RUNNING Py8nSystem. The caller owns the commit.

    Order matters: datasets first (knowledge + writes land on them), then
    the PROCESSES (v85: seeded from the datasets just built - the business
    state machine arrives pre-wired), then workflows (inactive - the boot
    door opens them; business_advance refs resolve to the BUILT process
    ids), the agent (rooms and queues bind it), rooms, queues, the
    campaign (composed directly - create_campaign refuses empty target
    lists by design), the dashboard (generated over the BUILT datasets),
    and finally the system with the durable installed operation + the
    system.installed event.
    """
    import pandas as pd

    from ..models import (Dashboard, Py8nSystem, SystemComponent, VoiceCampaign,
                          Workflow)
    from . import dashboards as dash_svc
    from . import datasets as ds_svc
    from . import business_processes as process_svc
    from . import system_runtime
    from . import voice_agents as va_svc
    from . import voice_campaigns as campaigns_svc
    from . import voice_meetings as meetings_svc
    from . import voice_queue as queue_svc
    from .versions import snapshot_workflow_version

    op = OPERATORS_BY_SLUG.get(slug)
    if op is None:
        raise OperatorError(f"unknown operator {slug!r}")
    brain = (brain or "scaffold").strip()
    if brain not in va_svc.BRAINS:
        raise OperatorError(f"brain must be {'|'.join(va_svc.BRAINS)}, got {brain!r}")
    if brain == "ai_agent" and not (llm_credential_id or "").strip():
        raise OperatorError("brain=ai_agent needs llm_credential_id "
                            "(the brain routes through a real provider credential)")

    built: dict = {"datasets": [], "processes": [], "workflows": [], "agents": [],
                   "rooms": [], "queues": [], "campaign": None, "dashboard": None,
                   "system": None}
    wiring_notes = list(op["notes"])
    ds_rows: list[tuple[object, object]] = []  # (Dataset, DataFrame) for the board
    ds_by_name: dict[str, dict] = {}
    ds_seed_rows: dict[str, list[dict]] = {}

    # ---- 1) datasets first (knowledge bindings and writes land on them) ---
    for d in op["datasets"]:
        cols = [str(c) for c in d["columns"]]
        rows = [r for r in (d["rows"] or []) if isinstance(r, dict)]
        df = pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)
        ds = await ds_svc.create_from_df(
            db, await _unique_dataset_name(db, d["name"]), df,
            source="operator",
            description=str(d.get("description") or "")[:500],
            owner_id=owner_id)
        ds_by_name[d["name"]] = {"id": ds.id, "name": ds.name}
        ds_rows.append((ds, df))
        ds_seed_rows[d["name"]] = rows
        built["datasets"].append({"id": ds.id, "name": ds.name,
                                  "columns": cols, "rows": len(rows)})

    # ---- 1.5) the PROCESSES (v85) - the machine arrives pre-wired and ----
    # SEEDED: one instance per seed row of the named dataset, starting at
    # the row's own stage (the CRM is imported, not rewound), ref = the
    # external key the business already tracks (the phone a call can match)
    proc_by_name: dict[str, dict] = {}
    for pspec in op.get("processes") or []:
        definition = dict(pspec.get("definition") or {})
        policy = _policy_for(pspec)  # v87: the channel + repeat policy rides the definition
        if policy:
            definition["escalation_policy"] = policy
        journeys = _journeys_for(pspec)  # v89: the legs this machine opens
        if journeys:
            definition["journeys"] = journeys
        proc = await process_svc.create_process(
            db, owner_id=owner_id, name=str(pspec["name"])[:140],
            description=str(pspec.get("description") or "")[:500],
            definition=definition)
        seeded = 0
        skipped_seed = 0
        src_name = str(pspec.get("seed_from_dataset") or "").strip()
        ref_col = str(pspec.get("ref_column") or "").strip()
        state_col = str(pspec.get("state_column") or "").strip()
        title_cols = [str(c) for c in (pspec.get("title_from") or [])]
        due_s = pspec.get("due_in_seconds")
        for row in ds_seed_rows.get(src_name, []):
            if not isinstance(row, dict):
                continue
            ref = str(row.get(ref_col) or "").strip() if ref_col else ""
            if not ref:
                skipped_seed += 1
                continue
            title = " - ".join(str(row.get(c) or "").strip()
                               for c in title_cols if str(row.get(c) or "").strip())
            begin = (str(row.get(state_col) or "").strip() if state_col else "") or None
            await process_svc.start_instance(
                db, proc["id"], owner_id=owner_id, ref=ref,
                title=title[:200], context=dict(row),
                due_in_seconds=int(due_s) if due_s else None,
                state=begin, actor="operator-install")
            seeded += 1
        if skipped_seed:
            wiring_notes.append(f"process {proc['name']!r}: {skipped_seed} seed row(s) "
                                "had no ref and were skipped honestly.")
        proc_by_name[pspec["name"]] = {"id": proc["id"], "name": proc["name"]}
        built["processes"].append({"id": proc["id"], "name": proc["name"],
                                   "states": proc["states"],
                                   "seeded_instances": seeded})

    # ---- 2) workflows (event-reactive, installed INACTIVE - honest) -------
    # dataset names wired into step params resolve to the BUILT names (a
    # second install of the same operator suffixes its datasets - the write
    # must land on the dataset THIS system owns)
    for w in op["workflows"]:
        steps = []
        for s in w.get("steps") or []:
            params = dict(s.get("params") or {})
            ref = str(params.get("dataset") or "").strip()
            if ref in ds_by_name:
                params["dataset"] = ds_by_name[ref]["name"]
            # v85 + v86 + v88: business_advance, business_start AND
            # business_onboard steps resolve the process by NAME in the
            # spec, but bind to the BUILT process id (a second install of
            # the same operator must move, track and onboard ITS pipeline,
            # never the first's)
            proc_ref = str(params.get("process") or "").strip()
            if (proc_ref and str(s.get("type") or "")
                    in ("business_advance", "business_start", "business_onboard")):
                hit = proc_by_name.get(proc_ref)
                if not hit:
                    raise OperatorError(
                        f"workflow {w['name']!r}: process {proc_ref!r} did not build")
                params["process"] = hit["id"]
            steps.append({**s, "params": params})
        graph = validate_graph_document(_workflow_graph({**w, "steps": steps})).model_dump()
        wf = Workflow(name=str(w["name"])[:200],
                      description=str(w.get("description") or "")[:500],
                      graph=graph, is_active=False)
        wf.owner_id = owner_id
        db.add(wf)
        await db.flush()
        await db.refresh(wf)
        await snapshot_workflow_version(db, wf)
        built["workflows"].append({"id": wf.id, "name": wf.name,
                                   "trigger": w["trigger"]["params"].get("event_type", ""),
                                   "active": False})

    # ---- 3) the agent (rooms and queues bind it) --------------------------
    agent_spec = op.get("agent") or {}
    agent_ref = None
    if agent_spec:
        kb = agent_spec.get("knowledge") if isinstance(agent_spec.get("knowledge"), dict) else {}
        kb_kwargs: dict = {}
        if kb and str(kb.get("dataset") or "").strip():
            ds_ref = ds_by_name.get(str(kb.get("dataset")).strip())
            if not ds_ref:
                raise OperatorError(f"knowledge dataset {kb.get('dataset')!r} did not build")
            kb_kwargs = {
                "knowledge_dataset_id": ds_ref["id"],
                "knowledge_text_column": kb.get("text_column"),
                "knowledge_answer_column": kb.get("answer_column"),
                "knowledge_top_k": max(1, min(int(kb.get("top_k") or 1), 5)),
            }
        try:
            va = await va_svc.create_agent(
                db, owner_id=owner_id,
                name=str(agent_spec["name"])[:140],
                description=f"Installed with the {op['name']} operator - " + (note or op["tagline"] or "")[:300],
                greeting_text=str(agent_spec.get("greeting") or "")[:400],
                system_prompt=str(agent_spec.get("system_prompt") or "")[:2000],
                scaffold_handler=True,
                brain=brain,
                brain_provider=("openai_compatible" if brain == "ai_agent" else "sandbox_bridge"),
                llm_credential_id=(llm_credential_id or None) if brain == "ai_agent" else None,
                **kb_kwargs)
        except va_svc.VoiceAgentError as exc:
            raise OperatorError(f"agent {agent_spec['name']!r} failed to build: {exc}") from exc
        agent_ref = va
        built["agents"].append({"id": va["id"], "name": va["name"],
                                "handler_workflow_id": va.get("handler_workflow_id"),
                                "knowledge": va.get("knowledge")})
    agent_id = (agent_ref or {}).get("id")

    # ---- 4) rooms (video-first when the operator says so) -----------------
    room_by_name: dict[str, dict] = {}
    for r in op["rooms"]:
        room = await meetings_svc.create_meeting(
            db, owner_id=owner_id, agent_id=agent_id,
            title=str(r.get("title") or r["name"])[:200])
        if str(r.get("modality") or "audio").lower() == "video":
            from ..models import VoiceMeeting

            row = await db.get(VoiceMeeting, room["id"])
            ctx = dict(row.context or {})
            ctx["modality"] = "audio+video"
            ctx["media_session_kind"] = "video"
            row.context = ctx
            db.add(row)
            await db.flush()
        room_by_name[r["name"]] = {"id": room["id"], "title": room["title"]}
        built["rooms"].append({"id": room["id"], "title": room["title"],
                               "modality": str(r.get("modality") or "audio")})

    # ---- 5) queues (seating into the built rooms) -------------------------
    for q in op["queues"]:
        room = room_by_name.get(str(q.get("room") or "").strip())
        if not room:
            raise OperatorError(f"queue {q['name']!r}: room {q.get('room')!r} did not build")
        try:
            queue = await queue_svc.create_queue(
                db, owner_id=owner_id, name=str(q["name"])[:140],
                meeting_id=room["id"],
                agent_id=agent_id if q.get("bind_agent") else None,
                config=dict(q.get("config") or {}))
        except queue_svc.VoiceQueueError as exc:
            raise OperatorError(f"queue {q['name']!r} failed to build: {exc}") from exc
        built["queues"].append({"id": queue["id"], "name": queue["name"],
                                "meeting": room["title"], "config": queue["config"]})

    # ---- 6) the campaign (composed directly: create_campaign refuses ------
    # empty target lists BY DESIGN - an operator ships an empty dialer the
    # team fills from the CRM; validate_config fills the retry/AMD defaults)
    camp_spec = op.get("campaign")
    if camp_spec:
        cfg = campaigns_svc.validate_config(dict(camp_spec.get("config") or {}))
        if not agent_id:
            raise OperatorError("a campaign operator needs its agent to build first")
        camp_row = VoiceCampaign(owner_id=owner_id, agent_id=agent_id,
                                 name=str(camp_spec["name"])[:140],
                                 endpoint_id=None, config=cfg)
        db.add(camp_row)
        await db.flush()
        await db.refresh(camp_row)
        built["campaign"] = {"id": camp_row.id, "name": camp_row.name,
                             "targets": 0, "config": cfg}
        wiring_notes.append("bind a telnyx voice endpoint on the campaign (channels page) "
                            "and add targets from the CRM - until then dials skip honestly.")

    # ---- 7) the STAFF DASHBOARD - generated over the BUILT datasets -------
    dash_spec = op["dashboard"]
    dash = Dashboard(
        name=await _unique_dashboard_name(db, str(dash_spec["name"])),
        slug=await dash_svc.unique_slug(db, dash_spec["name"]),
        description=str(dash_spec.get("description") or "")[:500],
        config=dash_svc.generate_config(ds_rows),
        status="draft")
    dash.owner_id = owner_id
    db.add(dash)
    await db.flush()
    built["dashboard"] = {"id": dash.id, "name": dash.name, "slug": dash.slug,
                          "components": len((dash.config or {}).get("components", []))}

    # ---- 8) THE SYSTEM - a running business entity from day one -----------
    sys_row = Py8nSystem(
        name=str(op["name"])[:140],
        description=f"Installed from the {op['name']} operator - " + (note or op["tagline"] or "")[:400],
        icon=op["icon"], color=op["color"])
    sys_row.owner_id = owner_id
    db.add(sys_row)
    await db.flush()
    for ds in built["datasets"]:
        db.add(SystemComponent(system_id=sys_row.id, kind="dataset", ref_id=ds["id"]))
    for wf in built["workflows"]:
        db.add(SystemComponent(system_id=sys_row.id, kind="workflow", ref_id=wf["id"]))
    for pr in built["processes"]:
        db.add(SystemComponent(system_id=sys_row.id, kind="process", ref_id=pr["id"]))
    for a in built["agents"]:
        db.add(SystemComponent(system_id=sys_row.id, kind="voice_agent", ref_id=a["id"]))
        if a.get("handler_workflow_id"):
            db.add(SystemComponent(system_id=sys_row.id, kind="workflow",
                                   ref_id=a["handler_workflow_id"]))
    for room in built["rooms"]:
        db.add(SystemComponent(system_id=sys_row.id, kind="meeting", ref_id=room["id"]))
    for q in built["queues"]:
        db.add(SystemComponent(system_id=sys_row.id, kind="queue", ref_id=q["id"]))
    if built["campaign"]:
        db.add(SystemComponent(system_id=sys_row.id, kind="campaign",
                               ref_id=built["campaign"]["id"]))
    db.add(SystemComponent(system_id=sys_row.id, kind="dashboard", ref_id=dash.id))
    await db.flush()

    comp_rows = (await db.execute(
        select(SystemComponent).where(SystemComponent.system_id == sys_row.id))).scalars().all()
    counts: dict[str, int] = {}
    for c in comp_rows:
        counts[c.kind] = counts.get(c.kind, 0) + 1
    await system_runtime.install_mark(
        db, sys_row, solution_slug=f"operator:{op['slug']}",
        actor=owner_id or "operator", component_counts=counts)

    built["system"] = {"id": sys_row.id, "name": sys_row.name,
                       "lifecycle": sys_row.lifecycle or "running",
                       "components": counts}
    built["notes"] = wiring_notes
    return built
