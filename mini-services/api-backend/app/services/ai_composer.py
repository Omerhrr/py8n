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
    "app": {
        "label": "App",
        "builds": ("a real Apps-builder App bound to a dataset in this spec - forms, a "
                   "records table, business rules and Excel export, published at /run/{slug} "
                   "(the exact same primitive the Apps builder UI creates, not a parallel one-off)"),
        "params": ["name", "description?", "dataset (dataset name in this spec)"],
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
    """v110: BEST-match archetype, not first-match.

    _ARCHETYPES is a fixed list; the old version returned the first entry
    with ANY matching keyword, so a clinic/appointment/patient description
    that also happened to mention "FAQ" (a support_line keyword, and a
    very generic one) got tagged "support_line" - wrong, and confusing
    anywhere the archetype label is shown or filtered on - even though
    the LLM's actual build was fine. Now every archetype is scored by how
    many of its keywords appear, and the highest score wins (ties keep
    the original list order, via stable sort)."""
    low = f" {(description or '').lower()} "
    scored = [(sum(1 for k in a["keywords"] if k in low), i, a["id"])
              for i, a in enumerate(_ARCHETYPES)]
    best_score, _, best_id = max(scored, key=lambda t: (t[0], -t[1]))
    return best_id if best_score > 0 else _FALLBACK_ARCHETYPE


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
    # v121: every archetype here composes TELEPHONY infrastructure (a voice
    # agent, a phone queue, SMS/callback backchannel) - but detect_archetype
    # can match on a single generic keyword ("support", "booking", "lead",
    # "pipeline"...) that appears in plenty of non-phone descriptions (a
    # data pipeline about "support tickets", a sales dataset with no
    # "call"/"phone"/"queue" intent at all). Flag it honestly instead of
    # silently handing back a full phone-support system for a request that
    # never asked for one.
    call_intent_words = ("call", "calls", "calling", "phone", "voice", "dial",
                         "ivr", "hotline", "hold", "queue", "sms", "text back",
                         "text message", "ring", "answer the phone", "voicemail",
                         "meeting", "zoom", "video", "webinar", "room")
    low_desc = f" {description.lower()} "
    if not any(w in low_desc for w in call_intent_words):
        note = (f"archetype={archetype!r} builds telephony (voice agent, phone "
                "queue, SMS backchannel), but this description never mentions "
                "calls, phone, voice or a queue - double-check this is what you "
                "wanted; a pure data/workflow request suits the AI System "
                "Builder (/builder) better.")
        # notes are truncated to 300 chars downstream (validate_spec) - keep
        # margin regardless of which archetype name gets interpolated (the
        # longest id, clinic_front_desk, is the worst case)
        assert len(note) <= 295, f"honesty note too long for the notes truncation ({len(note)} chars)"
        notes = list(notes) + [note]
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
    '{"kind": "app", "name": str, "description": str?, "dataset": str (dataset name in this spec)} '
    "- builds a real Apps-builder app over that dataset: a records table, an add/edit "
    "form and Excel export, auto-laid-out from the dataset's own columns (use this "
    "whenever the request talks about managing, editing, entering, or tracking "
    "records - invoicing, CRM, inventory, orders, tickets - not just storing them)\n"
    "Rules: 1-24 components; names unique; a queue's meeting and a room's agent "
    "must reference components IN THIS SPEC; a voice_agent's knowledge.dataset and "
    "an app's dataset must reference a dataset in this spec and its columns must exist on it; "
    "workflow step types must come from the allowed node types listed below; "
    "event_trigger needs params.event_type from the known event patterns. "
    "v117: step params must use each node's REAL schema, not an invented one - "
    '"filter" and "if_condition" take {"field"?: str (dot-path into the item; '
    "empty = whole item), \"operator\": one of equals|not_equals|contains|"
    "not_contains|greater_than|less_than|is_empty|not_empty|is_true|regex, "
    '"right_value"?: any} - there is NO free-form boolean-expression param on '
    "either node; express \"a and b\" as two chained steps, not one expression "
    "string. Template placeholders ({{ ... }}) may ONLY reference values that "
    "actually exist at runtime: {{ input.<field> }} or {{ input }} for the "
    "current item, {{ execution.trigger_payload.<field> }} for data the "
    "trigger received, {{ nodes.<step_name_or_id>.output.<field> }} for an "
    "earlier step's output, {{ env.<KEY> }} for an environment variable, "
    "{{ now }} for the current timestamp - NEVER invent a bare variable name "
    "like {{ last_week_start }} or {{ trigger.slot_start }} that isn't one of "
    "these; compute a derived value with a \"code\" step first if you need one. "
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
    messages = [{"role": "system", "content": _LLM_SYSTEM},
                {"role": "user", "content": _catalog_payload(description)}]
    # v110/v111: one bounded retry on a malformed-JSON reply, PLUS the real
    # root cause found while build-testing a 6-component clinic system:
    # max_tokens was capped at 1600 while the composer's own contract
    # allows up to MAX_COMPONENTS=24 richly-described components - a
    # spec anywhere near that size gets CUT OFF mid-JSON (the parse error's
    # character offset landed exactly at the ~1600-token mark, every time,
    # for the same description - not random sampling noise, truncation).
    # 3200 gives real headroom for a complex spec; stop_reason tells us
    # definitively whether a failure was truncation (in which case the
    # retry asks for something that FITS, not just "fix the syntax") or an
    # actual formatting slip (in which case the original repair prompt is
    # the right one).
    TRUNCATED = {"length", "max_tokens", "max_output_tokens"}
    max_tokens = 3200
    last_result: dict | None = None
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            result = asyncio.run(chat_completion(
                cred_data, model=model or "", messages=messages,
                temperature=0.2, max_tokens=max_tokens))
        except Exception as exc:  # LLMRoutingError or transport failure - loud
            raise AIComposerError(f"the LLM proposal failed: {exc}") from exc
        last_result = result
        try:
            spec = _extract_json(result["text"])
        except AIComposerError as exc:
            last_err = exc
            if attempt == 0:
                truncated = str(result.get("stop_reason") or "").lower() in TRUNCATED
                if truncated:
                    max_tokens = min(max_tokens * 2, 8000)
                    repair = ("That reply was CUT OFF before the JSON object finished "
                              f"(ran out of output length). Reply again with a MORE CONCISE "
                              "design (fewer components and/or shorter descriptions) so the "
                              "complete JSON object fits - still following every rule above.")
                else:
                    repair = (f"That reply was not valid JSON ({exc}). Reply again with ONLY "
                              "the corrected JSON object - no prose, no markdown fences.")
                messages = messages + [
                    {"role": "assistant", "content": result["text"][:4000]},
                    {"role": "user", "content": repair},
                ]
                continue
            raise
        # v111: JSON that parses but breaks a COMPOSER RULE (e.g. a trigger
        # type reused as a mid-workflow step) used to sail through this
        # function fine and only fail later, at the API layer's separate
        # validate_spec() call, as a 422 with no retry - one bad sample and
        # the user retypes the whole description. Give it the exact same
        # one-shot repair treatment as a JSON parse failure: validate HERE
        # too, and if it fails on the first attempt, tell the model
        # precisely which rule it broke and ask for a fix.
        try:
            validate_spec(spec)
            break
        except AIComposerError as exc:
            last_err = exc
            if attempt == 0:
                messages = messages + [
                    {"role": "assistant", "content": result["text"][:4000]},
                    {"role": "user", "content": f"That JSON parsed but is not a valid spec: {exc}. "
                                                "Reply again with a corrected JSON object that fixes "
                                                "exactly this, following every rule above."},
                ]
                continue
            break  # exhausted - hand the (still-invalid) spec back; the caller's own validate_spec raises the 422 as before
    else:  # pragma: no cover - loop always breaks or raises
        raise last_err or AIComposerError("the model's reply was not parseable JSON")
    result = last_result
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
    apps: dict[str, dict] = {}
    honesty_notes: list[str] = []

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
                sname_low = str(s.get("name") or "").lower()
                # v110: py8n has NO sms-send node (grep the registry - the
                # only outbound-notification node is email_send). A step
                # named "Send SMS ..." that resolves to email_send is a
                # real behavior mismatch, not just cosmetic - the run sends
                # an email, silently, while everything in the UI calls it
                # SMS. Surface it as a build note instead of staying quiet.
                if stype == "email_send" and ("sms" in sname_low or "text message" in sname_low):
                    honesty_notes.append(
                        f"Workflow {cname!r} step {s.get('name') or s_i!r}: named for SMS but py8n "
                        "has no SMS-send node yet, so this actually sends an EMAIL (email_send). "
                        "Rename it or wire a real SMS integration (e.g. an HTTP Request node to "
                        "Twilio/etc.) if a text message is actually required."
                    )
                if stype in TRIGGER_TYPES:
                    errors.append(f"workflow {cname!r}: step[{s_i}] {stype!r} is a trigger - "
                                  "a composed workflow has exactly ONE trigger, in trigger.type")
                elif stype not in COMPOSER_NODE_TYPES:
                    errors.append(f"workflow {cname!r}: step[{s_i}] node type {stype!r} is not "
                                  f"composable (allowed: {', '.join(COMPOSER_NODE_TYPES)})")
                elif get_node_class(stype) is None:
                    errors.append(f"workflow {cname!r}: step[{s_i}] node type {stype!r} is not "
                                  "registered in this build")
                else:
                    # v117: validate_spec never checked step["params"] against
                    # the node's OWN schema - only that the node type name was
                    # composable. Three separate LLM builds this session
                    # invented plausible-but-nonexistent param shapes (a
                    # Filter step with {"expression": "..."} when the real
                    # schema is field/operator/right_value; an If Condition
                    # step referencing {{trigger.slot_start}} when the real
                    # scope is {{execution.trigger_payload.slot_start}}) -
                    # every one of them BUILT successfully and then crashed
                    # on first run with a confusing TemplateResolutionError,
                    # because unresolved {{ }} placeholders in ANY param
                    # value get Jinja-resolved before the node ever gets a
                    # chance to reject the unknown key. Catch the unknown-key
                    # case at build time instead, with a message that names
                    # the actual allowed params - this also feeds the
                    # existing one-retry repair loop in
                    # propose_with_llm_sync(), so the LLM gets a chance to
                    # fix ITS OWN mistake before the user ever sees it.
                    node_cls = get_node_class(stype)
                    params = s.get("params")
                    if isinstance(params, dict) and node_cls is not None and node_cls.ParamsModel is not None:
                        allowed = set(node_cls.ParamsModel.model_fields.keys())
                        unknown = sorted(set(params.keys()) - allowed)
                        if unknown:
                            errors.append(
                                f"workflow {cname!r}: step[{s_i}] ({stype!r}) params has unknown "
                                f"field(s) {unknown} - allowed params for {stype!r} are "
                                f"{sorted(allowed)}"
                            )
                        else:
                            # v118: same bug class, one level deeper - the
                            # keys can be right and the VALUE shape still
                            # wrong (e.g. summarize's group_by wants a list,
                            # the LLM gave a bare string). Try constructing
                            # the real ParamsModel and surface genuine type
                            # errors at build time. A field whose raw value
                            # is still an unresolved {{ }} template is
                            # skipped - its real type is only known after
                            # Jinja resolves it at runtime, so it can't be
                            # judged here.
                            from pydantic import ValidationError as _PydanticValidationError
                            try:
                                node_cls.ParamsModel(**params)
                            except _PydanticValidationError as exc:
                                real_errors = []
                                for err in exc.errors(include_url=False):
                                    loc = err.get("loc") or ()
                                    key = loc[0] if loc else None
                                    raw = params.get(key) if key is not None else None
                                    if isinstance(raw, str) and "{{" in raw:
                                        continue
                                    real_errors.append(
                                        f"{'.'.join(str(x) for x in loc) or '(root)'}: {err.get('msg')}"
                                    )
                                if real_errors:
                                    errors.append(
                                        f"workflow {cname!r}: step[{s_i}] ({stype!r}) params are "
                                        "invalid - " + "; ".join(real_errors)
                                    )
                            # v119: same bug class, one level deeper still -
                            # summarize's sort_by/having[].label must name an
                            # ACTUAL output label, which is derived from
                            # group_by + aggregates (see SummarizeNode.execute:
                            # f"{field}_{op}" per aggregate, plus every
                            # group_by field, plus the always-present
                            # "_count"). The LLM invented "confirmed_count"
                            # when its own aggregates list never defined an
                            # aggregate that would produce that label - built
                            # fine, then NodeExecutionError'd on first run.
                            # This is deterministic (no node execution
                            # needed), so check it here instead of hoping.
                            if stype == "summarize":
                                raw_gb = params.get("group_by")
                                gb = [str(g) for g in raw_gb] if isinstance(raw_gb, list) else []
                                aggs = params.get("aggregates") or []
                                agg_labels = set()
                                if isinstance(aggs, list):
                                    for agg in aggs:
                                        if isinstance(agg, dict):
                                            op = str(agg.get("op") or "count")
                                            field = str(agg.get("field") or "")
                                            agg_labels.add(f"{field}_{op}" if field else op)
                                known_labels = set(gb) | agg_labels | {"_count"}
                                sort_by = str(params.get("sort_by") or "")
                                if sort_by and sort_by not in known_labels:
                                    errors.append(
                                        f"workflow {cname!r}: step[{s_i}] (summarize) sort_by "
                                        f"{sort_by!r} is not a label this step actually produces "
                                        f"(available: {sorted(known_labels)}) - it must be a "
                                        "group_by field or a \"field_op\" aggregate label"
                                    )
                                having = params.get("having") or []
                                if isinstance(having, list):
                                    for clause in having:
                                        if isinstance(clause, dict):
                                            hlabel = str(clause.get("label") or "")
                                            if hlabel and hlabel not in known_labels:
                                                errors.append(
                                                    f"workflow {cname!r}: step[{s_i}] (summarize) "
                                                    f"having label {hlabel!r} is not a label this "
                                                    f"step actually produces (available: "
                                                    f"{sorted(known_labels)})"
                                                )
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
        elif kind == "app":
            ds_name = str(c.get("dataset") or "").strip()
            if ds_name not in datasets:
                errors.append(f"app {cname!r}: dataset {ds_name!r} is not a dataset "
                              "in this spec (bind the app to a component you compose)")
            apps[cname] = c

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

    # v114: the Composer's schema (both deterministic archetype templates
    # AND the LLM-first prompt) has NO "report"/scheduled-summary component
    # kind at all - it only builds datasets, workflows, voice_agents,
    # meeting_rooms and queues. A description that asks for a "daily
    # summary"/"weekly digest"/report built NOTHING for that ask and said
    # nothing about the gap, unlike the SMS case above which at least
    # builds something (mislabeled). Surface it the same way: honestly,
    # once, pointing at the tool that actually has this capability.
    desc_low = str(spec.get("description") or "").lower()
    if re.search(r"\b(report|summary|digest|recap)\b", desc_low):
        honesty_notes.append(
            "This description mentions a report/summary, but the AI System Composer has no "
            "report component - it only builds datasets, workflows, voice agents, rooms and "
            "queues. Use the AI System Builder (/builder) for a scheduled report, or add one "
            "to a dataset here manually."
        )

    # v116: same silent-drop class as the report gap above, for third-party
    # messaging channels. py8n's only REAL outbound-notification nodes are
    # email_send and slack_message (grep COMPOSER_NODE_TYPES) - there is no
    # whatsapp/telegram/discord/messenger send capability anywhere in the
    # platform. A description asking for one gets nothing built for it and,
    # unlike "sms" (which at least gets flagged when a step is misnamed for
    # it), previously said nothing at all, because the deterministic
    # archetype templates never generate a step for these in the first
    # place - there was no step to inspect and flag.
    unsupported_channels = sorted({
        m for m in re.findall(r"\b(whatsapp|telegram|discord|messenger|instagram dm|imessage)\b", desc_low)
    })
    if unsupported_channels:
        honesty_notes.append(
            f"This description mentions {', '.join(unsupported_channels)}, but py8n has no "
            "integration for that - the only real outbound-notification channels are email "
            "(email_send) and Slack (slack_message). Wire an HTTP Request node to that "
            "provider's API yourself if you need it."
        )

    if errors:
        raise AIComposerError("invalid spec - " + " | ".join(errors[:12]))

    # v114 fix: build_system() calls validate_spec() a SECOND time on a spec
    # that came out of propose()/generate() - which already ran validate_spec
    # once and baked its honesty_notes into spec["notes"]. Re-scanning the
    # same description on the second pass re-appended the SAME honesty note,
    # so every "SMS is really email" / "no report component" note doubled up
    # in the built system's notes. De-dupe by text, preserving order, so a
    # note appears once no matter how many validation passes it survives.
    raw_notes = [str(n)[:300] for n in list(spec.get("notes") or []) + honesty_notes if str(n).strip()]
    seen_notes: set[str] = set()
    deduped_notes: list[str] = []
    for n in raw_notes:
        if n not in seen_notes:
            seen_notes.add(n)
            deduped_notes.append(n)

    return {
        "name": name[:140],
        "description": str(spec.get("description") or "").strip()[:500],
        "mode": str(spec.get("mode") or "validated"),
        "archetype": str(spec.get("archetype") or ""),
        "notes": deduped_notes[:12],
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
    from .graph_layout import _layout
    _layout(nodes, edges)
    return {"nodes": nodes, "edges": edges}


QUEUE_DEFAULTS = {"max_size": 20, "max_wait_seconds": 300}


async def build_system(db, spec: dict, *, owner_id: str | None,
                       default_llm_credential_id: str | None = None) -> dict:
    """Compose the spec into real primitives and bind them into a RUNNING
    Py8nSystem. The caller owns the commit. Returns built refs + notes."""
    import pandas as pd

    from ..engine.runner import validate_graph_document
    from ..models import Py8nSystem, SystemComponent, Workflow
    from . import apps as app_svc
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
                   "meeting_rooms": [], "queues": [], "apps": [], "system": None}

    # ---- 1) datasets first (knowledge bindings and writes land on them) ---
    ds_by_name: dict[str, dict] = {}
    ds_rows_by_name: dict[str, object] = {}
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
        ds_rows_by_name[name] = ds
        built["datasets"].append({"id": ds.id, "name": ds.name,
                                  "columns": cols, "rows": len(rows)})

    # ---- 1b) apps (forms + records + rules + Excel export, over a dataset
    # built above) - the exact same App primitive the Apps builder UI
    # creates, via the shared compose_app() helper.
    app_rows: list[dict] = []
    for c in spec["components"]:
        if c.get("kind") != "app":
            continue
        name = str(c.get("name")).strip()
        ds_name = str(c.get("dataset") or "").strip()
        ds_row = ds_rows_by_name.get(ds_name)
        if ds_row is None:
            raise AIComposerError(f"app {name!r}: dataset {ds_name!r} did not build")
        app_row = await app_svc.compose_app(
            db, name, ds_row,
            description=str(c.get("description") or "")[:500],
            owner_id=owner_id,
            publish=False,
        )
        row = {"id": app_row.id, "name": app_row.name, "slug": app_row.slug,
               "dataset": ds_by_name[ds_name]["name"]}
        app_rows.append(row)
        built["apps"].append(row)

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
        # v111: fall back to the SAME credential the user picked to propose
        # this spec (default_llm_credential_id, threaded in from /build and
        # /generate) when the component itself doesn't name one. Before
        # this, a brain=ai_agent voice_agent the LLM proposed - which
        # cannot know the vault's credential ids, so it never sets this
        # field itself - had NO path to build successfully: the frontend
        # already sent the chosen credential_id on build, but BuildRequest
        # had no field for it and FastAPI silently dropped it, so every
        # ai_agent-brained voice agent 400'd with no way to fix it in the UI.
        llm_cred = str(c.get("llm_credential_id") or "").strip() or (default_llm_credential_id or "")
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
    new_components = []
    for ds in built["datasets"]:
        c = SystemComponent(system_id=sys_row.id, kind="dataset", ref_id=ds["id"])
        db.add(c); new_components.append(c)
    for wf in built["workflows"]:
        c = SystemComponent(system_id=sys_row.id, kind="workflow", ref_id=wf["id"])
        db.add(c); new_components.append(c)
    for va in agent_rows:
        c = SystemComponent(system_id=sys_row.id, kind="voice_agent", ref_id=va["id"])
        db.add(c); new_components.append(c)
    for room in room_by_name.values():
        c = SystemComponent(system_id=sys_row.id, kind="meeting", ref_id=room["id"])
        db.add(c); new_components.append(c)
    for q in built["queues"]:
        c = SystemComponent(system_id=sys_row.id, kind="queue", ref_id=q["id"])
        db.add(c); new_components.append(c)
    for app_row in app_rows:
        c = SystemComponent(system_id=sys_row.id, kind="app", ref_id=app_row["id"])
        db.add(c); new_components.append(c)
    await db.flush()

    op = await record_operation(
        db, sys_row, "build", actor=owner_id or "composer",
        detail={"mode": spec.get("mode"),
                "components": {k: len(v) for k, v in built.items() if k != "system"},
                "note": "composed by the AI system builder (v82)"})
    # v113 fix: the aggregate "build" op above summarizes counts, but no
    # individual component_added entries were ever written for these
    # server-side binds - the Operations tab looked empty for every
    # component the Composer itself attached, unlike the interactive
    # POST /components door which always logs one row per bind. Write the
    # matching per-component rows so the audit trail is complete.
    for comp in new_components:
        comp_op = await record_operation(
            db, sys_row, "component_added", actor=owner_id or "composer",
            detail={"kind": comp.kind, "ref_id": comp.ref_id, "component_id": comp.id,
                    "via": "ai_system_composer"})
        await events_svc.emit(
            db, owner_id, "system.component_added", source="system",
            actor=owner_id or "composer", target_type="system", target_id=sys_row.id,
            payload={"operation_id": comp_op.id, "kind": comp.kind, "ref_id": comp.ref_id,
                     "system_id": sys_row.id, "system_name": sys_row.name},
            correlation_id=sys_row.id)
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
