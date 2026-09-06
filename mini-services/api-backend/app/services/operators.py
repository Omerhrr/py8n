"""Marketplace operators (v83) - "Install a business operator".

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
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..engine.runner import validate_graph_document


class OperatorError(ValueError):
    """Honest operator install failures."""


# ---------------------------------------------------------------------------
# The curated shelf - every operator declares the exact topology it builds
# ---------------------------------------------------------------------------

_MEETING_OPERATOR = {
    "slug": "meeting-operator",
    "name": "Meeting Operator",
    "tagline": ("A meeting business in one click: a video-first room with a concierge "
                "agent, a waiting-room queue with spoken positions, and meetings that "
                "log themselves to the notes board the moment they end."),
    "category": "Meetings",
    "icon": "video",
    "color": "#38bdf8",
    "outcomes": [
        "Video-first meeting room (in-app client)",
        "Meeting concierge AI agent",
        "Waiting-room queue (announce + SMS + callback wired)",
        "meeting.ended -> scribe workflow (event-reactive)",
        "Meeting notes dataset",
        "Staff dashboard over the notes",
    ],
    "datasets": [
        {"name": "Meeting notes",
         "description": "One row per ended meeting (written by the scribe workflow "
                        "the moment the room ends; recording_id points at the archive)",
         "columns": ["meeting_id", "title", "recording_id", "hung_up_legs", "ended_at"],
         "rows": [
             {"meeting_id": "seed", "title": "Weekly standup", "recording_id": "",
              "hung_up_legs": 4, "ended_at": "2026-01-05T09:31:00+00:00"},
             {"meeting_id": "seed", "title": "Design review", "recording_id": "",
              "hung_up_legs": 2, "ended_at": "2026-01-06T15:02:00+00:00"},
         ]},
    ],
    "workflows": [
        {"name": "Meeting scribe",
         "description": "meeting.ended -> shape -> Meeting notes. The room logs itself; "
                        "extend with an llm_chat step + a credential to auto-summarize.",
         "trigger": {"type": "event_trigger", "params": {"event_type": "meeting.ended"}},
         "steps": [
             {"type": "python_transform", "name": "Shape the ended meeting",
              "params": {"code": ("r = df.iloc[0] if len(df) else {}\n"
                                  "pl = r.get('payload') or {}\n"
                                  "result = [{'meeting_id': r.get('correlation_id', ''), "
                                  "'title': pl.get('title', ''), "
                                  "'recording_id': pl.get('recording_id') or '', "
                                  "'hung_up_legs': pl.get('hung_up_legs', 0), "
                                  "'ended_at': r.get('triggered_at', '')}]")}},
             {"type": "dataset_write", "name": "Write the note",
              "params": {"dataset": "Meeting notes", "mode": "append"}},
         ]},
    ],
    "agent": {"name": "Meeting concierge",
              "greeting": "Welcome - the meeting will begin momentarily.",
              "system_prompt": ("You are a meeting concierge: keep introductions moving, "
                                "note action items, hand the floor fairly."),
              "knowledge": None},
    "rooms": [{"name": "Meeting room", "title": "Operator meeting room",
               "modality": "video"}],
    "queues": [{"name": "Meeting waiting room", "room": "Meeting room", "bind_agent": True,
                "config": {"max_size": 20, "max_wait_seconds": 300,
                           "announce": {"enabled": True, "interval_seconds": 60},
                           "sms": {"enabled": True, "channel_id": "", "template": ""},
                           "callback": {"enabled": True, "endpoint_id": ""}}}],
    "campaign": None,
    "dashboard": {"name": "Meeting Operator Board",
                  "description": "The meeting business at a glance - ended-meeting volume, "
                                 "room sizes, archive pointers."},
    "notes": [
        "The room is video-first: join from the app's meeting client with mic and camera, "
        "record, and the transcript archives itself on end (v79 machinery).",
        "The waiting-room queue announces positions over hold and offers the SMS/callback "
        "backchannel - bind config.sms.channel_id and config.callback.endpoint_id on the "
        "queue when those credentials exist (until then the passes skip honestly).",
        "The scribe workflow reacts to the meeting.ended event - it installs INACTIVE; "
        "Boot the system (start with activate_workflows) to open the reactive path.",
    ],
}

_SALES_OPERATOR = {
    "slug": "sales-operator",
    "name": "Sales Operator",
    "tagline": ("A selling business in one click: a CRM, an SDR phone agent grounded in "
                "the sales FAQ, an outbound campaign ready for your dialing endpoint, "
                "leads that score themselves when calls end, and the pipeline board."),
    "category": "Sales",
    "icon": "trending-up",
    "color": "#34d399",
    "outcomes": [
        "CRM leads dataset (stages + scores)",
        "SDR phone agent grounded in the sales FAQ",
        "Outbound campaign (retry schedule + AMD defaults)",
        "call.ended -> lead scorer workflow (event-reactive)",
        "Lead events dataset (every scored call)",
        "Staff dashboard over the pipeline",
    ],
    "datasets": [
        {"name": "CRM leads",
         "description": "The pipeline - one row per lead; the scorer workflow appends "
                        "evidence to Lead events, the team moves stages here",
         "columns": ["name", "company", "phone", "stage", "score"],
         "rows": [
             {"name": "Dana Reyes", "company": "Globex", "phone": "+15550001111",
              "stage": "contacted", "score": 40},
             {"name": "Ari Cohen", "company": "Initech", "phone": "+15550001222",
              "stage": "interested", "score": 65},
             {"name": "Mia Silva", "company": "Umbrella", "phone": "+15550001333",
              "stage": "lead", "score": 20},
         ]},
        {"name": "Lead events",
         "description": "One row per scored call (written by the scorer workflow the "
                        "moment a call ends) - the evidence trail behind every score",
         "columns": ["session_id", "end_reason", "score", "scored_at"],
         "rows": []},
        {"name": "Sales FAQ",
         "description": "The SDR's knowledge - objections and product facts it can answer",
         "columns": ["question", "answer"],
         "rows": [
             {"question": "What does it cost",
              "answer": "The starter plan is twenty dollars a month; the team plan is fifty for up to five seats."},
             {"question": "Can I try before buying",
              "answer": "Yes - a fourteen day trial with the full feature set, no card required."},
             {"question": "Who is this for",
              "answer": "Teams that run their operations on the phone: support lines, clinics, field services."},
             {"question": "How does the callback feature work",
              "answer": "Callers trade the hold for a callback; the line dials them back and walks them into the room."},
             {"question": "Do you integrate with our phone provider",
              "answer": "The platform is provider-agnostic: Telnyx, Twilio, SIP, WhatsApp, Telegram, Discord or fully in-app."},
         ]},
    ],
    "workflows": [
        {"name": "Lead scorer",
         "description": "call.ended -> score -> Lead events. A completed call scores the "
                        "lead up; an abandoned one scores it down - deterministic evidence, "
                        "extend with an llm_chat step for transcript-based scoring.",
         "trigger": {"type": "event_trigger", "params": {"event_type": "call.ended"}},
         "steps": [
             {"type": "python_transform", "name": "Score the call",
              "params": {"code": ("r = df.iloc[0] if len(df) else {}\n"
                                  "pl = r.get('payload') or {}\n"
                                  "reason = pl.get('end_reason', '')\n"
                                  "score = 60 if reason in ('completed', 'hangup_by_caller') else (25 if reason else 10)\n"
                                  "result = [{'session_id': r.get('session_id', ''), "
                                  "'end_reason': reason, 'score': score, "
                                  "'scored_at': r.get('triggered_at', '')}]")}},
             {"type": "dataset_write", "name": "Append the lead event",
              "params": {"dataset": "Lead events", "mode": "append"}},
         ]},
    ],
    "agent": {"name": "Sales development rep",
              "greeting": "Hi! Calling about the trial you started - got two minutes?",
              "system_prompt": ("You are a warm, direct sales development rep. Answer from the "
                               "knowledge matches in metadata.knowledge; when a lead is busy, "
                               "offer the callback instead of pushing."),
              "knowledge": {"dataset": "Sales FAQ", "text_column": "question",
                            "answer_column": "answer", "top_k": 1}},
    "rooms": [{"name": "Sales room", "title": "Sales room", "modality": "audio"}],
    "queues": [],
    "campaign": {"name": "Sales follow-up campaign",
                 "config": {}},
    "dashboard": {"name": "Sales Operator Board",
                  "description": "The pipeline at a glance - leads by stage, scored calls, "
                                 "average lead score."},
    "notes": [
        "The campaign installs EMPTY on purpose: add targets from the CRM (POST "
        "/voice/campaigns/{id}/targets) and bind a telnyx voice endpoint when your "
        "dialing credentials exist - until then dials skip honestly.",
        "The scorer reacts to the call.ended event - it installs INACTIVE; Boot the "
        "system (start with activate_workflows) to open the reactive path.",
        "WhatsApp/Email channels are the installer's endpoints to bind (channels page) - "
        "the operator is the system underneath them, channels stay interchangeable.",
    ],
}

_CLINIC_OPERATOR = {
    "slug": "clinic-operator",
    "name": "Clinic Operator",
    "tagline": ("A clinic front desk in one click: an AI receptionist grounded in the "
                "clinic FAQ, a walk-in queue with the SMS backchannel, appointment "
                "requests that land from inbound texts, and the staff board."),
    "category": "Healthcare",
    "icon": "heart-pulse",
    "color": "#f472b6",
    "outcomes": [
        "Patient records dataset",
        "AI receptionist grounded in the clinic FAQ",
        "Consult room + walk-in queue (SMS backchannel on)",
        "sms.received -> appointment intake workflow (event-reactive)",
        "Clinic appointments dataset",
        "Staff dashboard over the front desk",
    ],
    "datasets": [
        {"name": "Clinic patients",
         "description": "The front desk's patient records - one row per patient",
         "columns": ["patient", "phone", "note"],
         "rows": [
             {"patient": "John Okoye", "phone": "+15550002111", "note": "annual checkup due"},
             {"patient": "Amara Eze", "phone": "+15550002222", "note": "follow-up, blood work"},
             {"patient": "Liam Byrne", "phone": "+15550002333", "note": "new patient intake"},
         ]},
        {"name": "Clinic appointments",
         "description": "Appointment requests - one row per inbound request (written by "
                        "the intake workflow the moment an SMS arrives)",
         "columns": ["queue_id", "keyword", "text", "action", "requested_at"],
         "rows": []},
        {"name": "Clinic FAQ",
         "description": "The receptionist's knowledge - hours, location, insurance, walk-ins",
         "columns": ["question", "answer"],
         "rows": [
             {"question": "What are your opening hours",
              "answer": "The clinic is open Monday to Friday eight AM to six PM, Saturdays nine AM to one PM."},
             {"question": "Do you take walk-ins",
              "answer": "Yes - walk-ins join the front desk queue and are seen in order; booking ahead is faster."},
             {"question": "Which insurance do you accept",
              "answer": "We accept the major national plans; bring your card and ID to the front desk."},
             {"question": "Where are you located",
              "answer": "Number twelve Clinic Avenue, ground floor - parking is available behind the building."},
             {"question": "How do I book an appointment",
              "answer": "Text this line any time and we will confirm your slot, or join the walk-in queue and ask the receptionist."},
         ]},
    ],
    "workflows": [
        {"name": "Appointment intake",
         "description": "sms.received -> shape -> Clinic appointments. Every inbound text "
                        "on the backchannel becomes an appointment request row.",
         "trigger": {"type": "event_trigger", "params": {"event_type": "sms.received"}},
         "steps": [
             {"type": "python_transform", "name": "Shape the request",
              "params": {"code": ("r = df.iloc[0] if len(df) else {}\n"
                                  "pl = r.get('payload') or {}\n"
                                  "result = [{'queue_id': pl.get('queue_id', ''), "
                                  "'keyword': pl.get('keyword', ''), "
                                  "'text': pl.get('text', ''), "
                                  "'action': pl.get('auto_answer_action', ''), "
                                  "'requested_at': r.get('triggered_at', '')}]")}},
             {"type": "dataset_write", "name": "Log the appointment request",
              "params": {"dataset": "Clinic appointments", "mode": "append"}},
         ]},
    ],
    "agent": {"name": "Clinic receptionist",
              "greeting": "Good day, this is the clinic front desk. How may I help?",
              "system_prompt": ("You are a calm clinic receptionist. Answer ONLY from the knowledge "
                               "matches in metadata.knowledge; for anything clinical, book a slot "
                               "or seat the caller - never give medical advice."),
              "knowledge": {"dataset": "Clinic FAQ", "text_column": "question",
                            "answer_column": "answer", "top_k": 1}},
    "rooms": [{"name": "Clinic consult room", "title": "Clinic consult room",
               "modality": "audio"}],
    "queues": [{"name": "Clinic front desk", "room": "Clinic consult room",
                "bind_agent": True,
                "config": {"max_size": 30, "max_wait_seconds": 600,
                           "announce": {"enabled": True, "interval_seconds": 90},
                           "sms": {"enabled": True, "channel_id": "", "template": ""},
                           "callback": {"enabled": True, "endpoint_id": ""}}}],
    "campaign": None,
    "dashboard": {"name": "Clinic Operator Board",
                  "description": "The front desk at a glance - appointment requests, patient "
                                 "records, walk-in pressure."},
    "notes": [
        "The front desk queue announces positions, texts waiting patients over the SMS "
        "backchannel (bind config.sms.channel_id when the credentials exist) and offers "
        "callbacks - the auto-answer keyword pass answers inbound texts either way.",
        "The intake workflow reacts to the sms.received event - it installs INACTIVE; "
        "Boot the system (start with activate_workflows) to open the reactive path.",
    ],
}

OPERATORS: list[dict] = [_MEETING_OPERATOR, _SALES_OPERATOR, _CLINIC_OPERATOR]
OPERATORS_BY_SLUG = {op["slug"]: op for op in OPERATORS}


def operator_catalog() -> dict:
    """The operators shelf - what each install BUILDS, counted honestly."""
    return {
        "operators": [
            {"slug": op["slug"], "name": op["name"], "tagline": op["tagline"],
             "category": op["category"], "icon": op["icon"], "color": op["color"],
             "outcomes": list(op["outcomes"]),
             "topology": {
                 "datasets": len(op["datasets"]),
                 "workflows": len(op["workflows"]),
                 "agents": 1 if op.get("agent") else 0,
                 "rooms": len(op["rooms"]),
                 "queues": len(op["queues"]),
                 "campaign": 1 if op.get("campaign") else 0,
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
        "installs": {
            "datasets": [{"name": d["name"], "description": d["description"],
                          "columns": list(d["columns"]), "rows": len(d["rows"])}
                         for d in op["datasets"]],
            "workflows": [{"name": w["name"], "description": w["description"],
                           "trigger": w["trigger"]["params"].get("event_type", "")}
                          for w in op["workflows"]],
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
    workflows (inactive - the boot door opens them), the agent (rooms and
    queues bind it), rooms, queues, the campaign (composed directly -
    create_campaign refuses empty target lists by design), the dashboard
    (generated over the BUILT datasets), and finally the system with the
    durable installed operation + the system.installed event.
    """
    import pandas as pd

    from ..models import (Dashboard, Py8nSystem, SystemComponent, VoiceCampaign,
                          Workflow)
    from . import dashboards as dash_svc
    from . import datasets as ds_svc
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

    built: dict = {"datasets": [], "workflows": [], "agents": [], "rooms": [],
                   "queues": [], "campaign": None, "dashboard": None, "system": None}
    wiring_notes = list(op["notes"])
    ds_rows: list[tuple[object, object]] = []  # (Dataset, DataFrame) for the board
    ds_by_name: dict[str, dict] = {}

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
        built["datasets"].append({"id": ds.id, "name": ds.name,
                                  "columns": cols, "rows": len(rows)})

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
