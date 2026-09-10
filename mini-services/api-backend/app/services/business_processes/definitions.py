"""Business processes: validate_definition, _terminal_states, _allowed_from.

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

