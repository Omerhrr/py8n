"""Business processes (v84) - long-running autonomy: the business state
machine.

Workflows are MOMENTS (trigger in, run, done). Businesses run on things
that stay open for days or months: a lead moving lead -> contacted ->
interested -> demo -> proposal -> negotiating -> won, a support case, an
insurance claim, an appointment, a delivery, a loan application. A
BusinessProcess is the MACHINE (states + named transitions); instances
are the tracked entities that REMEMBER state and context across weeks;
every advance is on the record (the transition log) and emits
business.state_changed through the v80 event door - so workflows and
agents react to the business moving ("proposal_sent -> send the
follow-up"; "stuck > SLA -> escalate"). Business state machine + agents +
workflows + data + interactions = long-running autonomy.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (BusinessProcess, BusinessProcessInstance,
                      BusinessProcessTransitionLog)
from . import escalations as escalations_svc  # v87: the channel + repeat policy layer


class ProcessError(ValueError):
    """Honest business-process failures."""


# v88: the paperwork rows the journey carries but the machines' move
# counts must not - annotations are memory writes, acknowledgements are
# the human's receipt, digest rows are the summary's receipts; the door's
# own no-move 'escalated' stays counted beside the machine's 'escalate'
# move (the v86 fix, untouched)
PAPERWORK_TRANSITIONS = frozenset({"annotate", "escalation_acknowledged",
                                   "escalation_digest"})
ANNOTATE_TRANSITION = "annotate"
ACK_TRANSITION = "escalation_acknowledged"
DIGEST_TRANSITION = "escalation_digest"
# the escalation door's episode bookkeeping lives at context.escalations -
# the annotate door (the AGENTS' write path) refuses to write under it
RESERVED_CONTEXT_KEYS = frozenset({"escalations"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# the definition - a validated state machine
# ---------------------------------------------------------------------------

def validate_definition(definition: dict | None) -> dict:
    """Validate the machine: states exist, transitions reference them, one
    initial state, no duplicate (name, from) pairs. Raises ProcessError."""
    d = definition if isinstance(definition, dict) else {}
    states = d.get("states")
    if not isinstance(states, list) or not states:
        raise ProcessError("definition.states must be a non-empty list of state names")
    clean_states: list[str] = []
    for s in states:
        s = str(s).strip()
        if not s:
            raise ProcessError("state names cannot be empty")
        if s in clean_states:
            raise ProcessError(f"duplicate state {s!r}")
        clean_states.append(s)
    initial = str(d.get("initial") or "").strip()
    if not initial:
        raise ProcessError("definition.initial is required")
    if initial not in clean_states:
        raise ProcessError(f"initial state {initial!r} is not in states {clean_states}")
    raw_transitions = d.get("transitions")
    if not isinstance(raw_transitions, list) or not raw_transitions:
        raise ProcessError("definition.transitions must be a non-empty list "
                           "- a machine with no moves is not a machine")
    clean_transitions: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for t in raw_transitions:
        t = t if isinstance(t, dict) else {}
        name = str(t.get("name") or "").strip()
        frm = str(t.get("from") or "").strip()
        to = str(t.get("to") or "").strip()
        if not name:
            raise ProcessError("every transition needs a name")
        if frm not in clean_states:
            raise ProcessError(f"transition {name!r}: 'from' state {frm!r} is not in states")
        if to not in clean_states:
            raise ProcessError(f"transition {name!r}: 'to' state {to!r} is not in states")
        if (name, frm) in seen:
            raise ProcessError(f"duplicate transition {name!r} from {frm!r}")
        seen.add((name, frm))
        clean_transitions.append({"name": name, "from": frm, "to": to,
                                  "description": str(t.get("description") or "")})
    out = {"states": clean_states, "initial": initial,
           "transitions": clean_transitions}
    # v87: an optional escalation policy rides the definition (channel +
    # repeat) - validated here so it cannot be smuggled in behind the
    # machine's back
    try:
        policy = escalations_svc.validate_escalation_policy(d.get("escalation_policy"))
    except escalations_svc.EscalationPolicyError as exc:
        raise ProcessError(str(exc)) from exc
    if policy:
        out["escalation_policy"] = policy
    # v89: cross-operator JOURNEYS ride the definition too - when this
    # machine lands on a named state, the next leg OPENS ITSELF on the
    # target machine (a won deal opening an onboarding case). Validated
    # loudly here (the fire-state must exist, the target must be named);
    # resolved at FIRE time by name - operators install independently, so
    # the target may arrive with a later install (the skip is then an
    # honest event, never a silent one).
    raw_journeys = d.get("journeys")
    if raw_journeys not in (None, []):
        if not isinstance(raw_journeys, list):
            raise ProcessError("definition.journeys must be a list of "
                               "{on_state, open} objects")
        clean_journeys: list[dict] = []
        seen_fires: set[str] = set()
        for j in raw_journeys:
            j = j if isinstance(j, dict) else {}
            unknown_j = sorted(set(j) - {"on_state", "open"})
            if unknown_j:
                raise ProcessError(f"journey has unknown key(s) {unknown_j} - "
                                   "allowed: ['on_state', 'open']")
            on_state = str(j.get("on_state") or "").strip()
            if on_state not in clean_states:
                raise ProcessError(f"journey fires on {on_state!r} - not a "
                                   f"state of this machine (states: {clean_states})")
            if on_state in seen_fires:
                raise ProcessError(f"duplicate journey firing on {on_state!r} - "
                                   "one journey per fire-state (the door refuses "
                                   "to guess which leg is real)")
            seen_fires.add(on_state)
            open_spec = j.get("open")
            if not isinstance(open_spec, dict):
                raise ProcessError(f"the journey on {on_state!r} needs an "
                                   "'open' object naming the target machine")
            unknown_o = sorted(set(open_spec) - {"process", "state",
                                                 "title_template",
                                                 "ref_template", "memory",
                                                 "due_in_seconds"})
            if unknown_o:
                raise ProcessError(f"journey open has unknown key(s) {unknown_o} - "
                                   "allowed: ['process', 'state', 'title_template', "
                                   "'ref_template', 'memory', 'due_in_seconds']")
            target = str(open_spec.get("process") or "").strip()
            if not target:
                raise ProcessError(f"the journey on {on_state!r} opens nothing - "
                                   "open.process is required (which machine opens "
                                   "the next leg?)")
            # v90: the opened leg can carry its own SLA promise - the door
            # (and its digests) watch the leg from the day it opens
            due_s = open_spec.get("due_in_seconds")
            if due_s is not None:
                try:
                    due_s = int(due_s)
                except (TypeError, ValueError):
                    raise ProcessError("journey open.due_in_seconds must be an "
                                       "integer of seconds") from None
                if due_s <= 0:
                    raise ProcessError("journey open.due_in_seconds must be > 0 "
                                       "(the SLA promise the opened leg starts with)")
            memory = open_spec.get("memory")
            if memory is not None and not isinstance(memory, dict):
                raise ProcessError("journey open.memory must be an object of "
                                   "{key: value} the opened instance starts with")
            clean_journeys.append({
                "on_state": on_state,
                "open": {"process": target[:140],
                         "state": (str(open_spec.get("state") or "").strip() or None),
                         "title_template": str(open_spec.get("title_template") or "").strip(),
                         "ref_template": str(open_spec.get("ref_template") or "").strip(),
                         "memory": dict(memory or {}),
                         "due_in_seconds": due_s}})
        out["journeys"] = clean_journeys
    return out


def _terminal_states(definition: dict) -> set[str]:
    outgoing = {t["from"] for t in definition["transitions"]}
    return {s for s in definition["states"] if s not in outgoing}


def _allowed_from(definition: dict, state: str) -> list[dict]:
    return [t for t in definition["transitions"] if t["from"] == state]


# ---------------------------------------------------------------------------
# process CRUD (definitions)
# ---------------------------------------------------------------------------

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
    from . import system_events as events_svc

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


# ---------------------------------------------------------------------------
# instances - the tracked entities that remember
# ---------------------------------------------------------------------------

def _aware(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes - normalize before arithmetic."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def instance_out(row: BusinessProcessInstance, *, definition: dict | None = None,
                 journey: list[dict] | None = None) -> dict:
    now = _now()
    entered = _aware(row.entered_state_at)
    age = (now - entered).total_seconds() if entered else 0
    terminal = sorted(_terminal_states(definition)) if definition else []
    out = {
        "id": row.id, "process_id": row.process_id, "ref": row.ref,
        "title": row.title, "state": row.state, "context": row.context or {},
        "entered_state_at": row.entered_state_at.isoformat() if row.entered_state_at else None,
        "due_at": row.due_at.isoformat() if row.due_at else None,
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "age_in_state_seconds": round(age),
        "is_terminal": bool(terminal) and row.state in terminal,
        "is_stuck": bool(row.due_at and row.ended_at is None and now > _aware(row.due_at)),
    }
    if journey is not None:
        out["journey"] = journey
    return out


def _log_out(t: BusinessProcessTransitionLog) -> dict:
    return {"id": t.id, "from_state": t.from_state, "to_state": t.to_state,
            "transition": t.transition, "actor": t.actor, "note": t.note,
            "payload": t.payload or {},
            "at": t.created_at.isoformat() if t.created_at else None}


async def _load_instance(db: AsyncSession, instance_id: str, owner_id: str | None) -> BusinessProcessInstance:
    row = await db.get(BusinessProcessInstance, instance_id)
    if row is None or (owner_id is not None and row.owner_id not in (owner_id, None)):
        raise ProcessError(f"instance {instance_id!r} not found")
    return row


async def start_instance(db: AsyncSession, process_id: str, *, owner_id: str | None,
                         ref: str, title: str = "", context: dict | None = None,
                         due_in_seconds: int | None = None,
                         actor: str = "",
                         state: str | None = None) -> dict:
    """Track a new entity. ``state`` optionally begins the journey at a
    named state instead of the initial one - how businesses IMPORT the
    entities they already track (a CRM lead imported as 'contacted' has
    not travelled the machine's road; the started row says so honestly).
    Everything else is validated like any other move."""
    p = await _load_process(db, process_id, owner_id)
    ref = str(ref or "").strip()
    if not ref:
        raise ProcessError("an instance needs a ref - the external key the "
                           "business already tracks (lead id, case number, phone)")
    definition = p.definition or {}
    begin = (definition.get("initial") or "")
    if state is not None:
        begin = str(state).strip()
        if begin not in (definition.get("states") or []):
            raise ProcessError(f"cannot start at {begin!r} - not a state of this "
                               f"machine (states: {definition.get('states')})")
    now = _now()
    row = BusinessProcessInstance(
        process_id=p.id, ref=ref[:180], title=(title or "").strip()[:200],
        state=begin,
        context=dict(context or {}),
        due_at=(now + timedelta(seconds=max(1, int(due_in_seconds))))
               if due_in_seconds else None)
    row.owner_id = owner_id
    row.entered_state_at = now
    db.add(row)
    await db.flush()
    await db.refresh(row)
    db.add(BusinessProcessTransitionLog(
        process_id=p.id, instance_id=row.id, from_state=None,
        to_state=row.state, transition="started", actor=(actor or "system")[:140],
        note="instance started (imported mid-machine)" if state is not None
             else "instance started",
        payload={"ref": row.ref}))
    return instance_out(row, definition=p.definition)


async def advance_instance(db: AsyncSession, instance_id: str, *, owner_id: str | None,
                           transition: str | None = None, to_state: str | None = None,
                           actor: str = "", note: str = "",
                           context_patch: dict | None = None,
                           payload: dict | None = None,
                           due_in_seconds: int | None = None) -> dict:
    """Move the instance. Resolve by transition name OR direct to_state;
    anything the machine does not allow is a loud refusal naming the
    allowed moves. The context patch MERGES into the running memory (fresh
    dicts - the JSON column is never mutated in place)."""
    row = await _load_instance(db, instance_id, owner_id)
    p = await _load_process(db, row.process_id, owner_id)
    definition = p.definition or {}
    current = row.state
    allowed = _allowed_from(definition, current)
    if not allowed:
        raise ProcessError(f"state {current!r} is terminal - the machine has "
                           "no outgoing moves; the journey is complete")
    chosen: dict | None = None
    if transition:
        t = str(transition).strip()
        chosen = next((x for x in allowed if x["name"] == t), None)
        if chosen is None:
            raise ProcessError(f"no transition {t!r} from state {current!r} - "
                               f"allowed: {[x['name'] for x in allowed]}")
    elif to_state:
        t = str(to_state).strip()
        chosen = next((x for x in allowed if x["to"] == t), None)
        if chosen is None:
            raise ProcessError(f"no move from {current!r} to {t!r} - "
                               f"allowed: {[(x['name'], x['to']) for x in allowed]}")
    else:
        raise ProcessError("name the move: transition (by name) or to_state")

    now = _now()
    new_ctx = dict(row.context or {})
    if context_patch:
        if not isinstance(context_patch, dict):
            raise ProcessError("context_patch must be an object")
        new_ctx.update(context_patch)
    row.context = new_ctx
    row.state = chosen["to"]
    row.entered_state_at = now
    if due_in_seconds is not None:
        row.due_at = now + timedelta(seconds=max(1, int(due_in_seconds)))
    terminal = _terminal_states(definition)
    row.ended_at = now if chosen["to"] in terminal else None
    db.add(row)
    log = BusinessProcessTransitionLog(
        process_id=p.id, instance_id=row.id, from_state=current,
        to_state=chosen["to"], transition=chosen["name"],
        actor=(actor or "system")[:140], note=(note or "")[:500],
        payload=dict(payload or {}))
    db.add(log)
    await db.flush()
    await db.refresh(row)

    # the business moved - the event system says so (workflows react)
    from . import system_events as events_svc

    await events_svc.emit(
        db, row.owner_id, "business.state_changed", source="business",
        actor=(actor or "system"), target_type="process_instance",
        target_id=row.id,
        payload={"process_id": p.id, "process_name": p.name,
                 "instance_id": row.id, "ref": row.ref, "title": row.title,
                 "from": current, "to": chosen["to"],
                 "transition": chosen["name"], "note": (note or "")[:200]},
        correlation_id=row.id)
    journey = await instance_journey(db, row.id)
    out = instance_out(row, definition=definition, journey=journey)
    # v89: the machine landed on a fire-state - the next leg opens itself
    opened = await fire_journeys(db, row, p, definition, to_state=chosen["to"],
                                 actor=(actor or "system"), now=now)
    if opened:
        out["journeys_opened"] = opened
    return out


async def instance_journey(db: AsyncSession, instance_id: str) -> list[dict]:
    rows = (await db.execute(
        select(BusinessProcessTransitionLog)
        .where(BusinessProcessTransitionLog.instance_id == instance_id)
        .order_by(BusinessProcessTransitionLog.created_at.asc()))).scalars().all()
    return [_log_out(t) for t in rows]


# ---------------------------------------------------------------------------
# cross-operator journeys (v89) - when this machine lands, the next leg opens
# ---------------------------------------------------------------------------

async def _resolve_process_ref(db: AsyncSession, want: str,
                               owner_id: str | None) -> BusinessProcess | None:
    """Find a process by id or case-insensitive name (most recent wins),
    owner-scoped - the same resolution the read door uses, because the
    journey's target is named at definition time and resolved when the
    machine actually lands (operators install independently)."""
    want = str(want or "").strip()
    if not want:
        return None
    prow = (await db.execute(
        select(BusinessProcess).where(BusinessProcess.id == want))).scalars().first()
    if prow is None:
        prow = (await db.execute(
            select(BusinessProcess)
            .where(func.lower(BusinessProcess.name) == want.lower())
            .order_by(BusinessProcess.created_at.desc()))).scalars().first()
    if prow is None:
        return None
    if owner_id is not None and prow.owner_id not in (owner_id, None):
        return None
    return prow


def _render_journey_template(template: str, fields: dict) -> str:
    """Fill a journey's title/ref template from the source instance's
    fields ({ref} {title} {state} {process}) - unknown placeholders stay
    literal, a broken or empty template renders empty (the caller falls
    back to the source's own values)."""
    if not template:
        return ""
    try:
        return str(template.format(**fields)).strip()
    except Exception:  # noqa: BLE001 - a broken template must not stop the deal
        return ""


async def fire_journeys(db: AsyncSession, row: BusinessProcessInstance,
                        process: BusinessProcess, definition: dict, *,
                        to_state: str, actor: str,
                        now: datetime) -> list[dict]:
    """The machine LANDED on a fire-state - open the next leg.

    Every journey keyed on to_state opens an instance on its target
    machine (resolved by name/id, owner-scoped): the ref/title render
    from the source's fields, the opened context carries the spec's
    memory plus the journey link (from_process / from_instance /
    from_ref / from_state), and the leg is on the record BOTH ways -
    business.journey_opened on the source's correlation thread, and an
    honest business.journey_skipped when the target machine does not
    exist yet (the target operator was never installed), already tracks
    an open instance with that ref (never double-tracks), or refuses the
    start (a bad state= naming). Journeys fire on ADVANCES only - the
    opened leg starts its own life and its own future fires; nothing
    recurses through the start itself."""
    journeys = [j for j in (definition.get("journeys") or [])
                if j.get("on_state") == to_state]
    if not journeys:
        return []
    from . import system_events as events_svc

    out: list[dict] = []
    for j in journeys:
        spec = j["open"]
        entry: dict = {"on_state": to_state, "target_process": spec["process"]}
        fields = {"ref": row.ref, "title": row.title, "state": to_state,
                  "process": process.name}
        ref = _render_journey_template(spec.get("ref_template"), fields) or row.ref
        title = _render_journey_template(spec.get("title_template"), fields) or row.title
        entry["ref"] = ref
        target = await _resolve_process_ref(db, spec["process"], row.owner_id)
        if target is None:
            entry["opened"] = False
            entry["reason"] = (f"target process {spec['process']!r} not found - "
                               "install the operator that ships it and the leg "
                               "opens on the next landing")
            await events_svc.emit(
                db, row.owner_id, "business.journey_skipped", source="business",
                actor=actor or "journey", target_type="process_instance",
                target_id=row.id,
                payload={"process_id": process.id, "process_name": process.name,
                         "instance_id": row.id, "ref": row.ref,
                         "title": row.title, "on_state": to_state,
                         "target_process": spec["process"], "reason": entry["reason"]},
                correlation_id=row.id)
            out.append(entry)
            continue
        dup = (await db.execute(
            select(BusinessProcessInstance)
            .where(BusinessProcessInstance.process_id == target.id,
                   BusinessProcessInstance.ref == ref,
                   BusinessProcessInstance.ended_at.is_(None)))).scalars().first()
        if dup is not None:
            entry["opened"] = False
            entry["reason"] = (f"an open instance of {target.name!r} already "
                               f"carries ref {ref!r} - the journey never "
                               "double-tracks")
            await events_svc.emit(
                db, row.owner_id, "business.journey_skipped", source="business",
                actor=actor or "journey", target_type="process_instance",
                target_id=row.id,
                payload={"process_id": process.id, "process_name": process.name,
                         "instance_id": row.id, "ref": row.ref,
                         "on_state": to_state, "target_process": target.name,
                         "target_ref": ref, "reason": entry["reason"]},
                correlation_id=row.id)
            out.append(entry)
            continue
        context = dict(spec.get("memory") or {})
        context["journey"] = {"from_process": process.name,
                              "from_instance": row.id,
                              "from_ref": row.ref,
                              "from_state": to_state,
                              "at": now.isoformat()}
        try:
            started = await start_instance(
                db, target.id, owner_id=row.owner_id, ref=ref,
                title=title[:200], context=context,
                state=spec.get("state") or None,
                due_in_seconds=spec.get("due_in_seconds"),
                actor=actor or "journey")
        except ProcessError as exc:
            entry["opened"] = False
            entry["reason"] = str(exc)
            await events_svc.emit(
                db, row.owner_id, "business.journey_skipped", source="business",
                actor=actor or "journey", target_type="process_instance",
                target_id=row.id,
                payload={"process_id": process.id, "process_name": process.name,
                         "instance_id": row.id, "ref": row.ref,
                         "on_state": to_state, "target_process": target.name,
                         "reason": entry["reason"][:300]},
                correlation_id=row.id)
            out.append(entry)
            continue
        entry["opened"] = True
        entry["target"] = {"process_id": target.id, "process_name": target.name,
                           "instance_id": started["id"], "ref": started["ref"],
                           "title": started["title"], "state": started["state"],
                           "due_at": started.get("due_at")}
        await events_svc.emit(
            db, row.owner_id, "business.journey_opened", source="business",
            actor=actor or "journey", target_type="process_instance",
            target_id=row.id,
            payload={"process_id": process.id, "process_name": process.name,
                     "instance_id": row.id, "ref": row.ref, "title": row.title,
                     "on_state": to_state, "target": entry["target"]},
            correlation_id=row.id)
        out.append(entry)
    return out


async def get_instance(db: AsyncSession, process_id: str, instance_id: str,
                       owner_id: str | None) -> dict:
    row = await _load_instance(db, instance_id, owner_id)
    if row.process_id != process_id:
        raise ProcessError(f"instance {instance_id!r} is not in process {process_id!r}")
    p = await _load_process(db, process_id, owner_id)
    journey = await instance_journey(db, row.id)
    return instance_out(row, definition=p.definition, journey=journey)


async def list_instances(db: AsyncSession, process_id: str, owner_id: str | None,
                         state: str | None = None) -> list[dict]:
    await _load_process(db, process_id, owner_id)
    q = (select(BusinessProcessInstance)
         .where(BusinessProcessInstance.process_id == process_id)
         .order_by(BusinessProcessInstance.entered_state_at.desc()))
    if state:
        q = q.where(BusinessProcessInstance.state == state)
    rows = (await db.execute(q)).scalars().all()
    p_def = (await _load_process(db, process_id, owner_id)).definition or {}
    return [instance_out(r, definition=p_def) for r in rows]


async def query_instances(db: AsyncSession, *, owner_id: str | None,
                          process: str | None = None, state: str | None = None,
                          ref: str | None = None, stuck_only: bool = False,
                          open_only: bool = True, limit: int = 50) -> dict:
    """Read the RUNNING entities across processes (v87) - the agents' door
    onto the operation. Resolve the process by id or case-insensitive name
    (most recent wins), filter by state / ref / stuck-ness, derive the
    per-instance view. Owner-scoped like every read: a NAMED query on
    another owner's process refuses loud (404-grade hiding), an unnamed
    one simply reads an empty operation."""
    limit = max(1, min(int(limit or 50), 500))
    process_name: str | None = None
    definitions: dict[str, dict] = {}
    names: dict[str, str] = {}

    q = select(BusinessProcessInstance)
    if process and str(process).strip():
        want = str(process).strip()
        prow = (await db.execute(
            select(BusinessProcess).where(BusinessProcess.id == want))).scalars().first()
        if prow is None:
            prow = (await db.execute(
                select(BusinessProcess)
                .where(func.lower(BusinessProcess.name) == want.lower())
                .order_by(BusinessProcess.created_at.desc()))).scalars().first()
        if prow is None or (owner_id is not None and prow.owner_id not in (owner_id, None)):
            raise ProcessError(f"process {process!r} not found")
        q = q.where(BusinessProcessInstance.process_id == prow.id)
        process_name = prow.name
        definitions[prow.id] = prow.definition or {}
        names[prow.id] = prow.name
    else:
        procs = (await db.execute(select(BusinessProcess))).scalars().all()
        for prow in procs:
            if owner_id is not None and prow.owner_id not in (owner_id, None):
                continue
            definitions[prow.id] = prow.definition or {}
            names[prow.id] = prow.name

    if owner_id is not None:
        q = q.where(BusinessProcessInstance.owner_id.in_((owner_id, None)))
    if state and str(state).strip():
        q = q.where(BusinessProcessInstance.state == str(state).strip())
    if ref and str(ref).strip():
        q = q.where(BusinessProcessInstance.ref == str(ref).strip())
    q = (q.order_by(BusinessProcessInstance.entered_state_at.desc())
          .limit(limit))
    rows = (await db.execute(q)).scalars().all()

    out = []
    for r in rows:
        if r.process_id not in definitions:
            continue  # another owner's process - never leak it
        o = instance_out(r, definition=definitions[r.process_id])
        o["process_name"] = names.get(r.process_id, "")
        if open_only and o["is_terminal"]:
            continue
        if stuck_only and not o["is_stuck"]:
            continue
        out.append(o)
    return {"instances": out, "count": len(out), "process": process_name,
            "filters": {"state": (state or "").strip(), "ref": (ref or "").strip(),
                        "stuck_only": bool(stuck_only), "open_only": bool(open_only),
                        "limit": limit}}


# ---------------------------------------------------------------------------
# the agents' write door (v88) - annotate the entity's memory, move nothing
# ---------------------------------------------------------------------------

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

    from . import system_events as events_svc

    await events_svc.emit(
        db, row.owner_id, "business.annotated", source="business",
        actor=who, target_type="process_instance", target_id=row.id,
        payload={"process_id": p.id, "process_name": p.name,
                 "instance_id": row.id, "ref": row.ref, "title": row.title,
                 "state": row.state, "keys": keys, "note": (note or "")[:200]},
        correlation_id=row.id)
    return instance_out(row, definition=p.definition,
                        journey=await instance_journey(db, row.id))


# ---------------------------------------------------------------------------
# the human's receipt (v88) - acknowledge the escalation, the door goes quiet
# ---------------------------------------------------------------------------

async def acknowledge_escalation(db: AsyncSession, process_id: str,
                                 instance_id: str, *, owner_id: str | None,
                                 by: str, note: str = "",
                                 snooze_hours: float | None = None) -> dict:
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
                                     snooze_hours=snooze_hours, now=_now())
    payload = {"attempt": int(book.get("count") or 0)}
    if ack.get("snooze_until"):
        payload["snooze_hours"] = ack["snooze_hours"]
        payload["snooze_until"] = ack["snooze_until"]
    db.add(BusinessProcessTransitionLog(
        process_id=p.id, instance_id=row.id, from_state=row.state,
        to_state=row.state, transition=ACK_TRANSITION,
        actor=who[:140], note=(note or "").strip()[:500]
        or "escalation acknowledged - the episode goes quiet",
        payload=payload))
    await db.flush()

    from . import system_events as events_svc

    await events_svc.emit(
        db, row.owner_id, "business.escalation_acknowledged", source="business",
        actor=who, target_type="process_instance", target_id=row.id,
        payload={"process_id": p.id, "process_name": p.name,
                 "instance_id": row.id, "ref": row.ref, "title": row.title,
                 "state": row.state, "acknowledged_by": who,
                 "note": (note or "")[:200],
                 **({k: ack[k] for k in ("snooze_hours", "snooze_until")
                     if k in ack})},
        correlation_id=row.id)
    return {"instance": instance_out(row, definition=p.definition,
                                     journey=await instance_journey(db, row.id)),
            "ack": ack}


# ---------------------------------------------------------------------------
# the process, measured - all derived, nothing stored twice
# ---------------------------------------------------------------------------

async def process_analytics(db: AsyncSession, process_id: str, owner_id: str | None) -> dict:
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
    return {
        "process_id": p.id, "name": p.name,
        "instances": len(instances), "open": open_count,
        "by_state": by_state,
        "stuck": stuck, "stuck_count": len(stuck),
        "escalations": escalations,
        "annotations": annotations,
        "acknowledgements": acknowledgements,
        "digests": digests,
        "mean_time_in_state_seconds": mean_time_in_state,
        "advance_counts": advance_counts,
        "terminal_states": sorted(terminal),
    }


# ---------------------------------------------------------------------------
# the attention feed (v91) - the overdue-attention view across ALL machines
# ---------------------------------------------------------------------------

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
            } if book else None,
            "escalation_summary": escalations_svc.describe_policy(
                escalations_svc.policy_from_definition(proc.definition or {})),
            "journey_leg": bool(ctx.get("journey")),
        })
    return {"attention": out, "count": len(out),
            "machines": len({r["process_id"] for r in out}),
            "now": now.isoformat()}


# ---------------------------------------------------------------------------
# v93: the SLA digest preview - the door's next move, rendered, never sent
# ---------------------------------------------------------------------------

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
                                 "snooze_remaining_seconds", "next_in_seconds")
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
                             "snooze_remaining_seconds", "next_in_seconds")
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
        items = [{"instance_id": b["instance_id"], "ref": b["ref"],
                  "title": b["title"], "state": b["state"],
                  "overdue_seconds": b["overdue_seconds"],
                  "overdue_minutes": b["overdue_minutes"],
                  "attempt": b["attempt"]} for b in bucket]
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


# ---------------------------------------------------------------------------
# the escalation door (v85) - the scheduler's sweep over stuck instances
# ---------------------------------------------------------------------------

ESCALATION_TRANSITION = "escalate"   # the move a machine may define for the door


async def _systems_holding(db: AsyncSession, process_ids: set[str]) -> dict[str, list[str]]:
    """process_id -> the lifecycles of the systems that bind it (v81 gate)."""
    if not process_ids:
        return {}
    from ..models import Py8nSystem, SystemComponent

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
                                                      "snooze_remaining_seconds")
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
                    from . import system_events as events_svc

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
                                                      "snooze_remaining_seconds")
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
        from . import system_events as events_svc

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
        items = [{"instance_id": m["row"].id, "ref": m["row"].ref,
                  "title": m["row"].title, "state": m["row"].state,
                  "overdue_seconds": m["overdue"],
                  "overdue_minutes": max(0, m["overdue"] // 60),
                  "attempt": m["attempt"]} for m in members]
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
