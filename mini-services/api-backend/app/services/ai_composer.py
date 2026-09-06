"""AI System Composer (v82) - describe a BUSINESS system, get one built.

The roadmap's AI System Builder stage: "Build me a customer support system"
becomes real primitives - a knowledge dataset, event-reactive workflows, a
voice agent, a meeting room, a channel-side queue - all bound into a RUNNING
Py8nSystem (v81). The killer difference the roadmap demands: the builder
NEVER generates a blob of code. It COMPOSES py8n primitives - every artifact
it creates is a normal, editable platform object created through the same
services the API itself uses.

The loop:

1. **synthesize_spec()** - the DETERMINISTIC composer: archetype templates
   keyed on the description's words (support line, meeting operator, lead
   desk, clinic front desk). The answer is testable and can never propose a
   primitive py8n cannot build - zero credentials required.
2. **propose_with_llm()** - LLM-first mode: the description plus the honest
   COMPOSER CATALOG go to a real provider credential through the v74 routing
   layer; the model returns a STRICT spec JSON which py8n VALIDATES against
   the actual node registry and the reference rules. An unreachable model or
   an unparseable reply falls back to the deterministic archetype with an
   honest note (AI proposes, py8n disposes, the builder never returns
   nothing). A REPLY THAT PARSES but proposes the impossible is refused loud
   - that is not a fallback situation, that is a hallucination to surface.
3. **build_system()** - spec -> real rows: datasets (with seed rows),
   workflows (real graphs from registered node types, imported inactive and
   activated loudly by the system's start door), meeting rooms (audio or
   video-first modality), knowledge-bound voice agents, channel queues with
   the announcement/SMS/callback wiring, and the Py8nSystem that binds it
   all with components + a durable operation + a ``system.*`` event on the
   platform's own correlation thread.
"""

from __future__ import annotations

import asyncio
import json
import re

from .llm_routing import chat_completion

# ---------------------------------------------------------------------------
# The composer catalog - the honest inventory the LLM (and the UI) sees.
# ---------------------------------------------------------------------------

COMPOSER_KINDS: dict[str, dict] = {
    "dataset": {
        "label": "Dataset",
        "builds": "a parquet-backed dataset (columns + optional seed rows)",
        "params": ["name", "description?", "columns[]", "rows? (list of objects)"],
    },
    "workflow": {
        "label": "Workflow",
        "builds": ("a real workflow graph: one trigger + a chain of registered "
                   "node types (composed, never code-generated)"),
        "params": ["name", "description?", "trigger {type, params?}", "steps[] {type, name?, params?}"],
    },
    "voice_agent": {
        "label": "Voice agent",
        "builds": ("a phone agent with greeting, persona and a knowledge "
                   "binding onto a dataset in this spec (deterministic "
                   "retrieval; brain=ai_agent rides an LLM credential)"),
        "params": ["name", "greeting", "system_prompt?", "brain (scaffold|ai_agent)",
                   "llm_credential_id? (brain=ai_agent)", "knowledge {dataset, text_column, answer_column, top_k?}"],
    },
    "meeting_room": {
        "label": "Meeting room",
        "builds": "a voice/video meeting room the queue seats callers into and agents staff",
        "params": ["name", "title?", "agent? (voice_agent name in this spec)", "modality? (audio|video)"],
    },
    "queue": {
        "label": "Channel queue",
        "builds": ("the waiting line: FIFO hold with derived positions, spoken "
                   "announcements, SMS backchannel and callback composition - "
                   "seats into a meeting room from this spec"),
        "params": ["name", "meeting (meeting_room name in this spec)", "agent?",
                   "max_size?", "max_wait_seconds?", "announce?", "sms_channel_id?", "callback_endpoint_id?"],
    },
}

TRIGGER_TYPES = ("manual_trigger", "schedule_trigger", "webhook_trigger",
                 "event_trigger", "dataset_trigger", "chat_trigger")

# the node types a spec's steps may use (composed chains stay small and legible)
COMPOSER_NODE_TYPES = (
    "manual_trigger", "schedule_trigger", "webhook_trigger", "event_trigger",
    "dataset_trigger", "chat_trigger",
    "code", "set_variable", "if_condition", "filter", "switch",
    "http_request", "llm_chat", "ai_agent", "email_send", "slack_message",
    "dataset_read", "dataset_write", "sql_query", "summarize", "sort",
    "remove_duplicates", "delay", "execute_workflow", "respond_to_webhook",
    "python_transform",
)

MAX_STEPS = 12
MAX_COMPONENTS = 24

BRAINS = ("scaffold", "ai_agent")


class AIComposerError(ValueError):
    """Raised for invalid descriptions, specs and build failures - loud."""


# ---------------------------------------------------------------------------
# The deterministic composer - archetype templates over the description.
# ---------------------------------------------------------------------------

_ARCHETYPES: list[dict] = [
    {
        "id": "support_line",
        "keywords": ("support", "help desk", "helpdesk", "customer service",
                     "hotline", "phone agent", "call center", "call centre",
                     "faq", "complaint"),
        "summary": "a phone support line: knowledge answers on the call, a waiting queue with announcements, SMS backchannel and callbacks",
    },
    {
        "id": "meeting_operator",
        "keywords": ("meeting", "zoom", "video call", "standup", "conference",
                     "webinar", "room system"),
        "summary": "a meeting operator: a video room, an agent persona, and an event-reactive workflow that logs every meeting into a notes dataset",
    },
    {
        "id": "lead_desk",
        "keywords": ("lead", "sales", "crm", "prospect", "follow up",
                     "follow-up", "pipeline", "deal"),
        "summary": "a lead desk: a webhook intake writing into a leads dataset, a follow-up queue and a voice agent for outbound-style callbacks",
    },
    {
        "id": "clinic_front_desk",
        "keywords": ("clinic", "appointment", "patient", "doctor", "dental",
                     "salon", "booking", "reservation"),
        "summary": "a front desk: an appointments dataset, a phone queue that seats into a staffed room, an agent that answers from the FAQ",
    },
]

_FALLBACK_ARCHETYPE = "support_line"


def detect_archetype(description: str) -> str:
    low = f" {(description or '').lower()} "
    for a in _ARCHETYPES:
        if any(k in low for k in a["keywords"]):
            return a["id"]
    return _FALLBACK_ARCHETYPE


def archetypes_out() -> list[dict]:
    return [{"id": a["id"], "summary": a["summary"],
             "keywords": list(a["keywords"])} for a in _ARCHETYPES]


def _faq_dataset(name: str, rows: list[dict]) -> dict:
    return {"kind": "dataset", "name": name,
            "description": f"Knowledge for the {name} - question/answer pairs the phone agent retrieves from",
            "columns": ["question", "answer"], "rows": rows}


def _archetype_spec(archetype: str, description: str) -> dict:
    """The archetype templates - every one is a VALID spec (build-proven)."""
    if archetype == "support_line":
        comps = [
            _faq_dataset("Support FAQ", [
                {"question": "What are your opening hours?",
                 "answer": "We are open Monday to Friday, 9 am to 6 pm."},
                {"question": "How do I reset my password?",
                 "answer": "Click 'Forgot password' on the sign-in page and follow the email link."},
                {"question": "Where is my order?",
                 "answer": "Tracking links are sent by email the moment your order ships."},
            ]),
            {"kind": "voice_agent", "name": "Support agent",
             "greeting": "Hello! You have reached support. How can I help?",
             "system_prompt": "You are a calm, precise phone support agent. Answer from the knowledge base; if the answer is not there, offer to take a message.",
             "brain": "scaffold",
             "knowledge": {"dataset": "Support FAQ", "text_column": "question",
                           "answer_column": "answer", "top_k": 1}},
            {"kind": "meeting_room", "name": "Support room", "title": "Support room",
             "agent": "Support agent", "modality": "audio"},
            {"kind": "queue", "name": "Support line", "meeting": "Support room",
             "agent": "Support agent", "max_size": 20, "max_wait_seconds": 300,
             "announce": True, "sms_channel_id": "", "callback_endpoint_id": ""},
            # v82 composition: the events system turns the queue into data
            {"kind": "workflow", "name": "Support call log",
             "description": "Every ended support call lands in its own dataset (event-reactive, v80 primitives composing)",
             "trigger": {"type": "event_trigger", "params": {"event_type": "call.ended"}},
             "steps": [
                 {"type": "python_transform", "name": "Shape the event",
                  "params": {"code": "r = df.iloc[0] if len(df) else {}\nresult = [{'event_type': r.get('type', ''), 'session_id': r.get('session_id', ''), 'correlation_id': r.get('correlation_id', ''), 'occurred_at': r.get('triggered_at', '')}]"}},
                 {"type": "dataset_write", "name": "Log the call",
                  "params": {"dataset": "Support calls", "mode": "append"}},
             ]},
            {"kind": "dataset", "name": "Support calls",
             "description": "One row per ended support call (written by the event-triggered log workflow)",
             "columns": ["event_type", "session_id", "correlation_id", "occurred_at"]},
        ]
        notes = [
            "The queue announces positions over hold and offers SMS/callback backchannel - "
            "bind config.sms.channel_id and config.callback.endpoint_id when those credentials exist "
            "(until then the passes record honest skips).",
            "brain=scaffold answers from the FAQ deterministically - flip the agent to brain=ai_agent "
            "with an LLM credential when you want model-routed answers.",
            "Workflows install INACTIVE - Start (or Boot) the system to open the reactive paths.",
        ]
    elif archetype == "meeting_operator":
        comps = [
            {"kind": "voice_agent", "name": "Meeting concierge",
             "greeting": "Welcome - the meeting will begin momentarily.",
             "system_prompt": "You are a meeting concierge: keep introductions moving, note action items.",
             "brain": "scaffold", "knowledge": {}},
            {"kind": "meeting_room", "name": "Standup room", "title": "Team standup room",
             "agent": "Meeting concierge", "modality": "video"},
            {"kind": "dataset", "name": "Meeting notes",
             "description": "One row per ended meeting (event-reactive log with the recording and transcript pointers)",
             "columns": ["event_type", "meeting_id", "correlation_id", "occurred_at"]},
            {"kind": "workflow", "name": "Meeting log",
             "description": "Every ended meeting lands in the notes dataset (meeting.ended event -> shape -> dataset write)",
             "trigger": {"type": "event_trigger", "params": {"event_type": "meeting.ended"}},
             "steps": [
                 {"type": "python_transform", "name": "Shape the event",
                  "params": {"code": "r = df.iloc[0] if len(df) else {}\nresult = [{'event_type': r.get('type', ''), 'meeting_id': r.get('target_id', ''), 'correlation_id': r.get('correlation_id', ''), 'occurred_at': r.get('triggered_at', '')}]"}},
                 {"type": "dataset_write", "name": "Log the meeting",
                  "params": {"dataset": "Meeting notes", "mode": "append"}},
             ]},
        ]
        notes = [
            "The room is video-first - join from the Meetings page with mic and camera, "
            "record, and the transcript archives itself on end.",
            "Extend the log workflow with an llm_chat step and a credential to have every "
            "meeting summarized on end.",
        ]
    elif archetype == "lead_desk":
        comps = [
            {"kind": "dataset", "name": "Leads",
             "description": "Inbound leads - written by the intake webhook, followed up by the desk",
             "columns": ["name", "email", "interest", "created_at"]},
            {"kind": "workflow", "name": "Lead intake",
             "description": "A webhook accepts lead JSON and appends it to the leads dataset",
             "trigger": {"type": "webhook_trigger", "params": {}},
             "steps": [
                 {"type": "dataset_write", "name": "Store the lead",
                  "params": {"dataset": "Leads", "mode": "append"}},
             ]},
            {"kind": "voice_agent", "name": "Lead agent",
             "greeting": "Hi! Thanks for your interest - let me help.",
             "system_prompt": "You are a friendly lead-qualification agent. Capture interest, confirm contact details.",
             "brain": "scaffold", "knowledge": {}},
            {"kind": "meeting_room", "name": "Sales room", "title": "Sales desk room",
             "agent": "Lead agent", "modality": "audio"},
            {"kind": "queue", "name": "Follow-up line", "meeting": "Sales room",
             "agent": "Lead agent", "max_size": 50, "max_wait_seconds": 600,
             "announce": True, "sms_channel_id": "", "callback_endpoint_id": ""},
        ]
        notes = [
            "Point your form/landing-page webhook at the Lead intake workflow - every POST becomes a dataset row.",
            "The system binds intake + agent + room + queue; bind SMS/callback credentials on the queue when they exist.",
        ]
    else:  # clinic_front_desk
        comps = [
            _faq_dataset("Clinic FAQ", [
                {"question": "Do I need an appointment?",
                 "answer": "Walk-ins are welcome, but booked appointments are seen first."},
                {"question": "What are your hours?",
                 "answer": "Weekdays 8 am to 5 pm, Saturdays 9 am to 1 pm."},
            ]),
            {"kind": "dataset", "name": "Appointments",
             "description": "Booked appointments the front desk works from",
             "columns": ["patient", "date", "time", "reason"]},
            {"kind": "voice_agent", "name": "Front desk agent",
             "greeting": "Good day, you have reached the front desk. How may I help?",
             "system_prompt": "You are the clinic front desk: answer questions from the FAQ, book and confirm appointments politely.",
             "brain": "scaffold",
             "knowledge": {"dataset": "Clinic FAQ", "text_column": "question",
                           "answer_column": "answer", "top_k": 1}},
            {"kind": "meeting_room", "name": "Front desk room", "title": "Front desk room",
             "agent": "Front desk agent", "modality": "audio"},
            {"kind": "queue", "name": "Patient line", "meeting": "Front desk room",
             "agent": "Front desk agent", "max_size": 30, "max_wait_seconds": 600,
             "announce": True, "sms_channel_id": "", "callback_endpoint_id": ""},
        ]
        notes = [
            "Callers hear their queue position; a reply '1' to the backchannel SMS leaves the line.",
            "Bind the SMS channel and callback endpoint on the queue when the credentials exist.",
        ]
    return {
        "name": _title_from(description) or archetype.replace("_", " ").title(),
        "description": description.strip()[:500],
        "archetype": archetype,
        "components": comps,
        "notes": notes,
    }


def _title_from(description: str) -> str:
    words = re.sub(r"[^\w\s]", " ", description or "").split()
    stop = {"a", "an", "the", "that", "which", "i", "we", "want", "need", "to",
            "and", "for", "my", "our", "build", "create", "make", "system",
            "me", "so", "can", "with", "set", "up"}
    picked = [w for w in words if w.lower() not in stop][:4]
    return " ".join(picked).strip().title()[:80]


def synthesize_spec(description: str) -> dict:
    """Deterministic description -> spec (archetype template). Never
    proposes anything py8n cannot build; needs no credentials."""
    text = (description or "").strip()
    if not text:
        raise AIComposerError("description is required")
    archetype = detect_archetype(text)
    spec = _archetype_spec(archetype, text)
    spec["mode"] = "deterministic"
    return spec


# ---------------------------------------------------------------------------
# The LLM-first proposal - the model composes the spec, py8n validates it.
# ---------------------------------------------------------------------------

_LLM_SYSTEM = (
    "You are py8n's system architect. The user describes a BUSINESS system; "
    "you compose it from py8n's primitives. You NEVER generate code - you "
    "return a STRICT JSON spec of components that py8n will build. Schema: "
    '{"name": str, "description": str, "components": [ ... ]} where every '
    "component is one of:\n"
    '{"kind": "dataset", "name": str, "description": str, "columns": [str], "rows": [object] (optional, max 5)}\n'
    '{"kind": "workflow", "name": str, "description": str, "trigger": {"type": "manual_trigger|schedule_trigger|webhook_trigger|event_trigger|dataset_trigger|chat_trigger", "params": object?}, "steps": [{"type": str, "name": str?, "params": object?}]} (max 12 steps)\n'
    '{"kind": "voice_agent", "name": str, "greeting": str, "system_prompt": str?, "brain": "scaffold"|"ai_agent", "llm_credential_id": str?, "knowledge": {"dataset": str, "text_column": str, "answer_column": str, "top_k"?: int}}\n'
    '{"kind": "meeting_room", "name": str, "title"?: str, "agent"?: str (voice_agent name), "modality"?: "audio"|"video"}\n'
    '{"kind": "queue", "name": str, "meeting": str (meeting_room name), "agent"?: str, "max_size"?: int, "max_wait_seconds"?: int, "announce"?: bool, "sms_channel_id"?: str, "callback_endpoint_id"?: str}\n'
    "Rules: 1-24 components; names unique; a queue's meeting and a room's agent "
    "must reference components IN THIS SPEC; a voice_agent's knowledge.dataset "
    "must reference a dataset in this spec and its columns must exist on it; "
    "workflow step types must come from the allowed node types listed below; "
    "event_trigger needs params.event_type from the known event patterns. "
    "No prose outside the JSON."
)


def _catalog_payload(description: str) -> str:
    kinds = "\n".join(f"- {k}: {v['builds']}" for k, v in COMPOSER_KINDS.items())
    return (
        f"Request: {description}\n\n"
        f"Components you may compose:\n{kinds}\n\n"
        f"Allowed workflow step node types: {', '.join(COMPOSER_NODE_TYPES)}\n"
        f"Known event patterns (examples): call.ended, call.waiting, "
        f"queue.position_changed, participant.joined, meeting.ended, "
        f"chat.posted, sms.received, recording.ready, callback.scheduled, system.*"
    )


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        raise AIComposerError("the model's reply contained no JSON object")
    try:
        data = json.loads(m.group(0))
    except ValueError as exc:
        raise AIComposerError(f"the model's reply was not parseable JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AIComposerError("the model's JSON was not an object")
    return data


def propose_with_llm_sync(description: str, cred_data: dict, *,
                          model: str = "", notes: list[str] | None = None) -> dict:
    """LLM-first proposal (sync - the API door runs it via asyncio.to_thread).

    Raises AIComposerError on ANY model failure (loud: the user chose the
    model); deterministic fallback is the CALLER's explicit choice.
    """
    try:
        # runs in a worker thread (asyncio.to_thread) - its own loop carries
        # the async routing call, exactly like the vault's decrypt_credential
        result = asyncio.run(chat_completion(
            cred_data, model=model or "",
            messages=[{"role": "system", "content": _LLM_SYSTEM},
                      {"role": "user", "content": _catalog_payload(description)}],
            temperature=0.2, max_tokens=1600))
    except Exception as exc:  # LLMRoutingError or transport failure - loud
        raise AIComposerError(f"the LLM proposal failed: {exc}") from exc
    spec = _extract_json(result["text"])
    spec["mode"] = "llm"
    spec["archetype"] = detect_archetype(description)
    if notes:
        spec.setdefault("notes", []).extend(
            f"AI ({result.get('label') or result.get('provider')}): {n}" for n in notes[:6])
    return spec


# ---------------------------------------------------------------------------
# Validation - the spec must only ever reference what py8n can really build.
# ---------------------------------------------------------------------------

def validate_spec(spec: dict) -> dict:
    """Validate a composed spec against the catalog, the node registry and
    the reference rules. Returns the normalized spec; raises AIComposerError
    listing EVERY problem (fail-loud, never a partial guess)."""
    if not isinstance(spec, dict):
        raise AIComposerError("spec must be an object")
    name = str(spec.get("name") or "").strip()
    if not name:
        raise AIComposerError("spec.name is required")
    comps = spec.get("components")
    if not isinstance(comps, list) or not comps:
        raise AIComposerError("spec.components must be a non-empty list")
    if len(comps) > MAX_COMPONENTS:
        raise AIComposerError(f"at most {MAX_COMPONENTS} components per spec (got {len(comps)})")

    from ..engine.registry import get_node_class

    errors: list[str] = []
    seen_names: dict[str, str] = {}
    datasets: dict[str, dict] = {}
    agents: dict[str, dict] = {}
    rooms: dict[str, dict] = {}
    queues: list[dict] = []
    workflows: list[dict] = []

    for i, raw in enumerate(comps):
        c = raw if isinstance(raw, dict) else {}
        kind = str(c.get("kind") or "").strip()
        cname = str(c.get("name") or "").strip()
        if kind not in COMPOSER_KINDS:
            errors.append(f"component[{i}]: unknown kind {kind!r} "
                          f"(composable kinds: {', '.join(sorted(COMPOSER_KINDS))})")
            continue
        if not cname:
            errors.append(f"component[{i}] ({kind}): name is required")
            continue
        if cname in seen_names:
            errors.append(f"component[{i}]: duplicate name {cname!r} (also a {seen_names[cname]})")
            continue
        seen_names[cname] = kind

        if kind == "dataset":
            cols = c.get("columns")
            if not isinstance(cols, list) or not cols or not all(
                    isinstance(x, str) and x.strip() for x in cols):
                errors.append(f"dataset {cname!r}: columns must be a non-empty list of column names")
                continue
            if len(set(cols)) != len(cols):
                errors.append(f"dataset {cname!r}: duplicate column names")
            rows = c.get("rows") or []
            if not isinstance(rows, list):
                errors.append(f"dataset {cname!r}: rows must be a list of objects")
            else:
                for r_i, r in enumerate(rows[:50]):
                    if not isinstance(r, dict):
                        errors.append(f"dataset {cname!r}: rows[{r_i}] is not an object")
            datasets[cname] = {"columns": [str(x).strip() for x in cols],
                               "rows": rows if isinstance(rows, list) else []}

        elif kind == "workflow":
            trig = c.get("trigger") if isinstance(c.get("trigger"), dict) else {}
            ttype = str(trig.get("type") or "").strip()
            if ttype not in TRIGGER_TYPES:
                errors.append(f"workflow {cname!r}: trigger.type must be one of "
                              f"{', '.join(TRIGGER_TYPES)} (got {ttype!r})")
            steps = c.get("steps")
            if not isinstance(steps, list):
                errors.append(f"workflow {cname!r}: steps must be a list")
                steps = []
            if len(steps) > MAX_STEPS:
                errors.append(f"workflow {cname!r}: at most {MAX_STEPS} steps (got {len(steps)})")
            for s_i, s in enumerate(steps[:MAX_STEPS]):
                s = s if isinstance(s, dict) else {}
                stype = str(s.get("type") or "").strip()
                if stype in TRIGGER_TYPES:
                    errors.append(f"workflow {cname!r}: step[{s_i}] {stype!r} is a trigger - "
                                  "a composed workflow has exactly ONE trigger, in trigger.type")
                elif stype not in COMPOSER_NODE_TYPES:
                    errors.append(f"workflow {cname!r}: step[{s_i}] node type {stype!r} is not "
                                  f"composable (allowed: {', '.join(COMPOSER_NODE_TYPES)})")
                elif get_node_class(stype) is None:
                    errors.append(f"workflow {cname!r}: step[{s_i}] node type {stype!r} is not "
                                  "registered in this build")
            workflows.append({"name": cname, "trigger": trig, "steps": steps,
                              "description": str(c.get("description") or "")})

        elif kind == "voice_agent":
            brain = str(c.get("brain") or "scaffold").strip()
            if brain not in BRAINS:
                errors.append(f"voice_agent {cname!r}: brain must be {'|'.join(BRAINS)} (got {brain!r})")
            kb = c.get("knowledge") if isinstance(c.get("knowledge"), dict) else {}
            if kb:
                ds_name = str(kb.get("dataset") or "").strip()
                if ds_name not in datasets:
                    errors.append(f"voice_agent {cname!r}: knowledge.dataset {ds_name!r} is not a "
                                  "dataset in this spec (bind knowledge to a component you compose)")
            agents[cname] = c
        elif kind == "meeting_room":
            rooms[cname] = c
        elif kind == "queue":
            queues.append(c)

    for q in queues:
        qname = str(q.get("name") or "")
        target = str(q.get("meeting") or "").strip()
        if target not in rooms:
            errors.append(f"queue {qname!r}: meeting {target!r} is not a meeting_room in this spec")
        agent_ref = str(q.get("agent") or "").strip()
        if agent_ref and agent_ref not in agents:
            errors.append(f"queue {qname!r}: agent {agent_ref!r} is not a voice_agent in this spec")

    for rname, r in rooms.items():
        agent_ref = str(r.get("agent") or "").strip()
        if agent_ref and agent_ref not in agents:
            errors.append(f"meeting_room {rname!r}: agent {agent_ref!r} is not a voice_agent in this spec")

    for aname, a in agents.items():
        kb = a.get("knowledge") if isinstance(a.get("knowledge"), dict) else {}
        if kb:
            ds_name = str(kb.get("dataset") or "").strip()
            if ds_name in datasets:
                cols = datasets[ds_name]["columns"]
                text_col = str(kb.get("text_column") or "").strip()
                ans_col = str(kb.get("answer_column") or "").strip()
                if text_col and text_col not in cols:
                    errors.append(f"voice_agent {aname!r}: knowledge.text_column {text_col!r} "
                                  f"is not a column of dataset {ds_name!r} ({', '.join(cols)})")
                if ans_col and ans_col not in cols:
                    errors.append(f"voice_agent {aname!r}: knowledge.answer_column {ans_col!r} "
                                  f"is not a column of dataset {ds_name!r} ({', '.join(cols)})")

    if errors:
        raise AIComposerError("invalid spec - " + " | ".join(errors[:12]))

    return {
        "name": name[:140],
        "description": str(spec.get("description") or "").strip()[:500],
        "mode": str(spec.get("mode") or "validated"),
        "archetype": str(spec.get("archetype") or ""),
        "notes": [str(n)[:300] for n in (spec.get("notes") or []) if str(n).strip()][:12],
        "components": comps,
    }


# ---------------------------------------------------------------------------
# The build - spec -> real py8n primitives, bound into a RUNNING system.
# ---------------------------------------------------------------------------

async def _unique_dataset_name(db, base: str) -> str:
    from . import datasets as ds_svc

    name = re.sub(r"\s+", " ", base).strip()[:100] or "Composer Dataset"
    if not ds_svc.NAME_RE.match(name):
        name = "Composer Dataset"
    candidate = name
    n = 1
    while await ds_svc.name_taken(db, candidate):
        n += 1
        candidate = f"{name} {n}"
    return candidate


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
    nodes.append(_node("n_trigger", str(trig.get("type") or "manual_trigger"),
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


QUEUE_DEFAULTS = {"max_size": 20, "max_wait_seconds": 300}


async def build_system(db, spec: dict, *, owner_id: str | None) -> dict:
    """Compose the spec into real primitives and bind them into a RUNNING
    Py8nSystem. The caller owns the commit. Returns built refs + notes."""
    import pandas as pd

    from ..engine.runner import validate_graph_document
    from ..models import Py8nSystem, SystemComponent, Workflow
    from . import datasets as ds_svc
    from . import system_events as events_svc
    from . import voice_agents as va_svc
    from . import voice_meetings as meetings_svc
    from . import voice_queue as queue_svc
    from .system_runtime import record_operation
    from .versions import snapshot_workflow_version

    spec = validate_spec(spec)
    notes = list(spec.get("notes") or [])
    built: dict = {"datasets": [], "workflows": [], "voice_agents": [],
                   "meeting_rooms": [], "queues": [], "system": None}

    # ---- 1) datasets first (knowledge bindings and writes land on them) ---
    ds_by_name: dict[str, dict] = {}
    for c in spec["components"]:
        if c.get("kind") != "dataset":
            continue
        name = str(c.get("name")).strip()
        cols = [str(x).strip() for x in (c.get("columns") or [])]
        rows = [r for r in (c.get("rows") or []) if isinstance(r, dict)]
        df = pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)
        ds = await ds_svc.create_from_df(
            db, await _unique_dataset_name(db, name), df,
            source="ai_composer",
            description=str(c.get("description") or "")[:500],
            owner_id=owner_id)
        ds_by_name[name] = {"id": ds.id, "name": ds.name}
        built["datasets"].append({"id": ds.id, "name": ds.name,
                                  "columns": cols, "rows": len(rows)})

    # ---- 2) workflows (composed graphs, imported inactive - honest) -------
    # dataset names wired into step params are resolved to the BUILT names
    # (global uniqueness may suffix them - the write must land on the
    # dataset this system actually owns)
    def _resolved_steps(steps: list) -> list:
        out: list = []
        for s in steps:
            s = s if isinstance(s, dict) else {}
            params = dict(s.get("params") or {})
            for key in ("dataset", "dead_letter_dataset"):
                v = str(params.get(key) or "").strip()
                if v in ds_by_name:
                    params[key] = ds_by_name[v]["name"]
            out.append({**s, "params": params})
        return out

    for c in spec["components"]:
        if c.get("kind") != "workflow":
            continue
        name = str(c.get("name")).strip()
        graph_spec = {**c, "steps": _resolved_steps(c.get("steps") or [])}
        graph = validate_graph_document(_workflow_graph(graph_spec)).model_dump()
        wf = Workflow(name=name[:200],
                      description=str(c.get("description") or "")[:500],
                      graph=graph, is_active=False)
        wf.owner_id = owner_id
        db.add(wf)
        await db.flush()
        await db.refresh(wf)
        await snapshot_workflow_version(db, wf)
        built["workflows"].append({"id": wf.id, "name": wf.name,
                                   "nodes": len(graph.get("nodes", [])),
                                   "trigger": (c.get("trigger") or {}).get("type")})

    # ---- 3) voice agents (scaffolded handlers; knowledge bound HERE) ------
    # agents build BEFORE rooms so the rooms can bind the agent directly
    agent_rows: list[dict] = []
    for c in spec["components"]:
        if c.get("kind") != "voice_agent":
            continue
        name = str(c.get("name")).strip()
        brain = str(c.get("brain") or "scaffold").strip()
        llm_cred = str(c.get("llm_credential_id") or "").strip()
        if brain == "ai_agent" and not llm_cred:
            raise AIComposerError(
                f"voice_agent {name!r}: brain=ai_agent needs llm_credential_id "
                "(the brain routes through a real provider credential)")
        kb = c.get("knowledge") if isinstance(c.get("knowledge"), dict) else {}
        kb_kwargs: dict = {}
        if kb and str(kb.get("dataset") or "").strip():
            ds_ref = ds_by_name.get(str(kb.get("dataset")).strip())
            if not ds_ref:
                raise AIComposerError(
                    f"voice_agent {name!r}: knowledge dataset {kb.get('dataset')!r} did not build")
            kb_kwargs = {
                "knowledge_dataset_id": ds_ref["id"],
                "knowledge_text_column": str(kb.get("text_column") or "").strip() or None,
                "knowledge_answer_column": str(kb.get("answer_column") or "").strip() or None,
                "knowledge_top_k": max(1, min(int(kb.get("top_k") or 1), 5)),
            }
        try:
            va = await va_svc.create_agent(
                db, owner_id=owner_id,
                name=name[:140],
                description=f"Composed by the AI system builder for {spec['name']!r}",
                greeting_text=str(c.get("greeting") or "")[:400],
                system_prompt=str(c.get("system_prompt") or "")[:2000],
                scaffold_handler=True,
                brain=brain,
                brain_provider=("openai_compatible" if brain == "ai_agent" else "sandbox_bridge"),
                llm_credential_id=(llm_cred or None) if brain == "ai_agent" else None,
                **kb_kwargs)
        except va_svc.VoiceAgentError as exc:
            raise AIComposerError(f"voice_agent {name!r} failed to build: {exc}") from exc
        row = {"spec_name": name, "id": va["id"], "name": va["name"],
               "handler_workflow_id": va.get("handler_workflow_id"),
               "knowledge": va.get("knowledge")}
        agent_rows.append(row)
        built["voice_agents"].append(row)

    # ---- 4) meeting rooms (video-first when the spec says video) ----------
    room_by_name: dict[str, dict] = {}
    for c in spec["components"]:
        if c.get("kind") != "meeting_room":
            continue
        name = str(c.get("name")).strip()
        agent_ref = str(c.get("agent") or "").strip()
        ag = next((a for a in agent_rows if a["spec_name"] == agent_ref), None)
        modality = str(c.get("modality") or "audio").strip().lower()
        if modality not in ("audio", "video"):
            modality = "audio"
        room = await meetings_svc.create_meeting(
            db, owner_id=owner_id, agent_id=(ag["id"] if ag else None),
            title=str(c.get("title") or name)[:200])
        if modality == "video":
            from ..models import VoiceMeeting
            row = await db.get(VoiceMeeting, room["id"])
            ctx = dict(row.context or {})
            ctx["modality"] = "audio+video"
            ctx["media_session_kind"] = "video"
            row.context = ctx
            db.add(row)
            await db.flush()
        room_by_name[name] = {"id": room["id"], "title": room["title"]}
        built["meeting_rooms"].append({"id": room["id"], "title": room["title"],
                                       "modality": modality,
                                       "agent": agent_ref or None})

    # ---- 5) queues (seating into the built rooms) -------------------------
    for c in spec["components"]:
        if c.get("kind") != "queue":
            continue
        name = str(c.get("name")).strip()
        room = room_by_name.get(str(c.get("meeting") or "").strip())
        if not room:
            raise AIComposerError(
                f"queue {name!r}: meeting {c.get('meeting')!r} did not build")
        agent_ref = str(c.get("agent") or "").strip()
        ag = next((a for a in agent_rows if a["spec_name"] == agent_ref), None)
        cfg = {
            "max_size": int(c.get("max_size") or QUEUE_DEFAULTS["max_size"]),
            "max_wait_seconds": int(c.get("max_wait_seconds") or QUEUE_DEFAULTS["max_wait_seconds"]),
            "announce": {"enabled": bool(c.get("announce", True))},
            "sms": {"enabled": True,
                    "channel_id": str(c.get("sms_channel_id") or ""),
                    "template": ""},
            "callback": {"enabled": True,
                         "endpoint_id": str(c.get("callback_endpoint_id") or "")},
        }
        try:
            q = await queue_svc.create_queue(
                db, owner_id=owner_id, name=name[:140],
                meeting_id=room["id"], agent_id=(ag["id"] if ag else None),
                config=cfg)
        except queue_svc.VoiceQueueError as exc:
            raise AIComposerError(f"queue {name!r} failed to build: {exc}") from exc
        built["queues"].append({"id": q["id"], "name": q["name"],
                                "meeting": room["title"],
                                "agent": agent_ref or None,
                                "config": q["config"]})

    # ---- 6) THE SYSTEM - a running entity from day one --------------------
    sys_row = Py8nSystem(
        name=str(spec["name"])[:140],
        description=(str(spec.get("description") or "")
                     or "Composed by the AI system builder")[:500],
        icon="sparkles", color="#6366f1")
    sys_row.owner_id = owner_id
    db.add(sys_row)
    await db.flush()
    for ds in built["datasets"]:
        db.add(SystemComponent(system_id=sys_row.id, kind="dataset", ref_id=ds["id"]))
    for wf in built["workflows"]:
        db.add(SystemComponent(system_id=sys_row.id, kind="workflow", ref_id=wf["id"]))
    for va in agent_rows:
        db.add(SystemComponent(system_id=sys_row.id, kind="voice_agent", ref_id=va["id"]))
    for room in room_by_name.values():
        db.add(SystemComponent(system_id=sys_row.id, kind="meeting", ref_id=room["id"]))
    for q in built["queues"]:
        db.add(SystemComponent(system_id=sys_row.id, kind="queue", ref_id=q["id"]))
    await db.flush()

    op = await record_operation(
        db, sys_row, "build", actor=owner_id or "composer",
        detail={"mode": spec.get("mode"),
                "components": {k: len(v) for k, v in built.items() if k != "system"},
                "note": "composed by the AI system builder (v82)"})
    await events_svc.emit(
        db, owner_id, "system.built", source="system",
        actor=owner_id or "composer", target_type="system", target_id=sys_row.id,
        payload={"system_id": sys_row.id, "system_name": sys_row.name,
                 "operation_id": op.id, "mode": spec.get("mode"),
                 "components": sum(len(v) for k, v in built.items() if k != "system")},
        correlation_id=sys_row.id)

    sys_row_out = {"id": sys_row.id, "name": sys_row.name,
                   "lifecycle": sys_row.lifecycle or "running",
                   "components": sum(len(v) for k, v in built.items() if k != "system")}
    built["system"] = sys_row_out

    if not any("INACTIVE" in n.upper() for n in notes):
        notes.append("Workflows install INACTIVE - Start (or Boot with activate_workflows) "
                     "the system to open its reactive paths; the agents' scaffolded handlers "
                     "answer live calls either way.")
    built["notes"] = notes
    built["spec"] = {"name": spec["name"], "mode": spec.get("mode"),
                     "archetype": spec.get("archetype")}
    return built
