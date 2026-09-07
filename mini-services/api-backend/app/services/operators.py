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
        "Meeting lifecycle process (pre-wired, email escalations)",
        "Meeting notes dataset",
        "Staff dashboard over the notes",
    ],
    "datasets": [
        {"name": "Meeting notes",
         "description": "One row per ended meeting (written by the scribe workflow "
                        "the moment the room ends; recording_id points at the archive)",
         "columns": ["meeting_id", "title", "recording_id", "hung_up_legs", "ended_at", "stage"],
         "rows": [
             {"meeting_id": "MTG-1001", "title": "Weekly standup", "recording_id": "",
              "hung_up_legs": 4, "ended_at": "2026-01-05T09:31:00+00:00",
              "stage": "notes_logged"},
             {"meeting_id": "MTG-1002", "title": "Design review", "recording_id": "",
              "hung_up_legs": 2, "ended_at": "2026-01-06T15:02:00+00:00",
              "stage": "notes_logged"},
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
    "processes": [
        {"name": "Meeting lifecycle",
         "description": ("The meeting business's state machine - every tracked meeting "
                         "from invite to follow-up; a meeting stuck past its SLA escalates "
                         "over email and the door walks it through the machine's own "
                         "escalate moves."),
         "definition": {
             "states": ["scheduled", "invited", "confirmed", "in_progress",
                        "held", "notes_logged", "followed_up", "cancelled"],
             "initial": "scheduled",
             "transitions": [
                 {"name": "invite", "from": "scheduled", "to": "invited"},
                 {"name": "confirm", "from": "invited", "to": "confirmed"},
                 {"name": "start", "from": "confirmed", "to": "in_progress"},
                 {"name": "end", "from": "in_progress", "to": "held"},
                 {"name": "log_notes", "from": "held", "to": "notes_logged"},
                 {"name": "follow_up", "from": "notes_logged", "to": "followed_up"},
                 {"name": "cancel", "from": "scheduled", "to": "cancelled"},
                 {"name": "cancel", "from": "invited", "to": "cancelled"},
                 {"name": "cancel", "from": "confirmed", "to": "cancelled"},
                 # the escalation door's move - a meeting can go stale at
                 # ANY working stage
                 {"name": "escalate", "from": "scheduled", "to": "scheduled",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "invited", "to": "invited",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "confirmed", "to": "confirmed",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "in_progress", "to": "in_progress",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "held", "to": "held",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "notes_logged", "to": "notes_logged",
                  "description": "SLA breach nudge - the door's move"},
             ],
         },
         "seed_from_dataset": "Meeting notes",
         "ref_column": "meeting_id",
         "title_from": ["title"],
         "state_column": "stage",
         "due_in_seconds": 24 * 3600,
        },
    ],
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
        "The Meeting lifecycle machine ships pre-wired (seeded at the ended-meeting "
        "stage from the notes) and its escalation policy tells the desk over EMAIL - "
        "bind escalation_policy.to + a channel endpoint (channels page); until then "
        "the escalations land on the event timeline only.",
    ],
}

_SALES_OPERATOR = {
    "slug": "sales-operator",
    "name": "Sales Operator",
    "tagline": ("A selling business in one click: a CRM, an SDR phone agent grounded in "
                "the sales FAQ, an outbound campaign ready for your dialing endpoint, "
                "a lead pipeline that calls move by themselves, and the staff board."),
    "category": "Sales",
    "icon": "trending-up",
    "color": "#34d399",
    "outcomes": [
        "CRM leads dataset (stages + scores)",
        "Lead pipeline business process (pre-wired, seeded from the CRM)",
        "SDR phone agent grounded in the sales FAQ",
        "Outbound campaign (retry schedule + AMD defaults)",
        "call.ended -> lead scorer + pipeline advancer (event-reactive)",
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
        {"name": "Pipeline advancer",
         "description": "call.ended -> business_advance: a completed call moves the caller's "
                        "pipeline instance forward (lead -> contacted). Callers the pipeline "
                        "does not track, and leads already past the move, skip honestly - "
                        "the call is evidence, not a forced move.",
         "trigger": {"type": "event_trigger", "params": {"event_type": "call.ended"}},
         "steps": [
             {"type": "business_advance", "name": "Move the caller's lead",
              "params": {"process": "Lead pipeline", "ref": "{{ input.actor }}",
                         "transition": "reach_out", "actor": "sales-operator",
                         "note": "a real call ended - the pipeline moves itself",
                         "on_missing": "skip", "on_refusal": "skip"}},
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
    "processes": [
        {"name": "Lead pipeline",
         "description": ("The selling journey as a state machine - one tracked instance per "
                         "lead remembering stage and context across weeks; calls move it, "
                         "the escalation door nudges it when it goes stale."),
         "definition": {
             "states": ["lead", "contacted", "interested", "demo_booked",
                        "demo_completed", "proposal_sent", "negotiating", "won", "lost"],
             "initial": "lead",
             "transitions": [
                 {"name": "reach_out", "from": "lead", "to": "contacted"},
                 {"name": "qualify", "from": "contacted", "to": "interested"},
                 {"name": "book_demo", "from": "interested", "to": "demo_booked"},
                 {"name": "run_demo", "from": "demo_booked", "to": "demo_completed"},
                 {"name": "send_proposal", "from": "demo_completed", "to": "proposal_sent"},
                 {"name": "negotiate", "from": "proposal_sent", "to": "negotiating"},
                 {"name": "win", "from": "negotiating", "to": "won"},
                 {"name": "lose", "from": "contacted", "to": "lost"},
                 {"name": "lose", "from": "interested", "to": "lost"},
                 {"name": "lose", "from": "proposal_sent", "to": "lost"},
                 {"name": "lose", "from": "negotiating", "to": "lost"},
                 # the escalation door's move: a stale deal re-enters its state
                 # (a fresh stint, on the record) so the team sees the nudge
                 {"name": "escalate", "from": "interested", "to": "interested",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "proposal_sent", "to": "proposal_sent",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "negotiating", "to": "negotiating",
                  "description": "SLA breach nudge - the door's move"},
             ],
         },
         "seed_from_dataset": "CRM leads",
         "ref_column": "phone",
         "title_from": ["name", "company"],
         "state_column": "stage",
         "due_in_seconds": 7 * 24 * 3600,
        },
    ],
    "dashboard": {"name": "Sales Operator Board",
                  "description": "The pipeline at a glance - leads by stage, scored calls, "
                                 "average lead score."},
    "notes": [
        "The campaign installs EMPTY on purpose: add targets from the CRM (POST "
        "/voice/campaigns/{id}/targets) and bind a telnyx voice endpoint when your "
        "dialing credentials exist - until then dials skip honestly.",
        "The lead pipeline installs SEEDED from the CRM (one instance per lead, at its "
        "CRM stage, ref = the phone) and the advancer reacts to call.ended: a completed "
        "call from a tracked number moves the lead forward by itself.",
        "The scorer + advancer react to the call.ended event - they install INACTIVE; "
        "Boot the system (start with activate_workflows) to open the reactive path.",
        "Stale deals: the machine defines escalate self-loops on interested / "
        "proposal_sent / negotiating - the scheduler door (POST "
        "/scheduler/escalations/tick) walks past-SLA instances through them.",
        "Won deals hand themselves off: the pipeline carries a cross-operator "
        "journey (won -> Customer onboarding) - install the Operations operator "
        "too and the onboarding case OPENS ITSELF at kickoff, ref = the lead's "
        "phone; until then the landing skips honestly (business.journey_skipped "
        "names the missing machine).",
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
        "Appointment journey process (pre-wired, SMS escalations)",
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
    "processes": [
        {"name": "Appointment journey",
         "description": ("The front desk's state machine - every tracked appointment "
                         "from request to completion; an unconfirmed request stuck past "
                         "its SLA escalates over SMS and the door walks it through the "
                         "machine's own escalate moves."),
         "definition": {
             "states": ["requested", "confirmed", "checked_in", "in_consult",
                        "billed", "completed", "no_show"],
             "initial": "requested",
             "transitions": [
                 {"name": "confirm", "from": "requested", "to": "confirmed"},
                 {"name": "check_in", "from": "confirmed", "to": "checked_in"},
                 {"name": "begin_consult", "from": "checked_in", "to": "in_consult"},
                 {"name": "bill", "from": "in_consult", "to": "billed"},
                 {"name": "complete", "from": "billed", "to": "completed"},
                 {"name": "no_show", "from": "confirmed", "to": "no_show"},
                 {"name": "no_show", "from": "checked_in", "to": "no_show"},
                 # the escalation door's move - an appointment can go
                 # stale at ANY working stage
                 {"name": "escalate", "from": "requested", "to": "requested",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "confirmed", "to": "confirmed",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "checked_in", "to": "checked_in",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "in_consult", "to": "in_consult",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "billed", "to": "billed",
                  "description": "SLA breach nudge - the door's move"},
             ],
         },
         "seed_from_dataset": "Clinic patients",
         "ref_column": "phone",
         "title_from": ["patient"],
         "due_in_seconds": 12 * 3600,
        },
    ],
    "dashboard": {"name": "Clinic Operator Board",
                  "description": "The front desk at a glance - appointment requests, patient "
                                 "records, walk-in pressure."},
    "notes": [
        "The front desk queue announces positions, texts waiting patients over the SMS "
        "backchannel (bind config.sms.channel_id when the credentials exist) and offers "
        "callbacks - the auto-answer keyword pass answers inbound texts either way.",
        "The intake workflow reacts to the sms.received event - it installs INACTIVE; "
        "Boot the system (start with activate_workflows) to open the reactive path.",
        "The Appointment journey ships pre-wired (seeded per patient, ref = the "
        "phone the clinic texts) and its escalation policy tells the desk over SMS "
        "- bind escalation_policy.to + a channel endpoint; until then the "
        "escalations land on the event timeline only.",
        "Cross-operator journeys fire here: landing on 'billed' opens the visit's "
        "invoice on the Finance operator's machine (ref = the patient's phone, "
        "the leg carries its own SLA) - the visit is on the payable machine "
        "before the patient reaches the parking lot.",
    ],
}

_SUPPORT_OPERATOR = {
    "slug": "support-operator",
    "name": "Support Operator",
    "tagline": ("A support desk in one click: an AI support agent grounded in the "
                "support FAQ, a help queue with the SMS backchannel, tickets that "
                "open themselves from inbound texts and land in the case machine "
                "tracked, and the staff board."),
    "category": "Support",
    "icon": "headset",
    "color": "#60a5fa",
    "outcomes": [
        "Support tickets dataset (cases + statuses)",
        "Case lifecycle business process (pre-wired, seeded from the tickets)",
        "Support agent grounded in the support FAQ",
        "Help queue (announce + SMS + callback wired)",
        "sms.received -> ticket opener (event-reactive: row AND tracked case)",
        "call.ended -> case logger + case advancer (event-reactive)",
        "Ticket events dataset (every touch)",
        "Staff dashboard over the desk",
    ],
    "datasets": [
        {"name": "Support tickets",
         "description": "The desk - one row per case; the opener workflow appends "
                        "inbound requests, the team works the statuses",
         "columns": ["ticket", "requester", "phone", "subject", "status"],
         "rows": [
             {"ticket": "TCK-1041", "requester": "Nora Faye", "phone": "+15550003111",
              "subject": "cannot sign in", "status": "assigned"},
             {"ticket": "TCK-1042", "requester": "Omar Diallo", "phone": "+15550003222",
              "subject": "refund not received", "status": "investigating"},
             {"ticket": "TCK-1043", "requester": "June Park", "phone": "+15550003333",
              "subject": "password reset", "status": "opened"},
         ]},
        {"name": "Ticket events",
         "description": "One row per touch (written by the logger workflow the moment "
                        "a call ends) - the traffic behind the cases",
         "columns": ["session_id", "channel", "action", "at"],
         "rows": []},
        {"name": "Support FAQ",
         "description": "The agent's knowledge - the answers the desk gives all day",
         "columns": ["question", "answer"],
         "rows": [
             {"question": "I cannot sign in",
              "answer": "Use the forgot-password link on the sign-in page; the reset mail arrives within a minute. If it does not, we open a case and engineering looks the same day."},
             {"question": "What is the refund policy",
              "answer": "Full refund within fourteen days of purchase, no questions asked - after that we prorate to the day and credit the account."},
             {"question": "What are your support hours",
              "answer": "The line is staffed eight AM to eight PM weekdays; the AI agent answers any time and a human picks up urgent cases around the clock."},
             {"question": "How do I reach a human",
              "answer": "Say agent at any point on the call, or reply HUMAN to the SMS thread - you keep your queue place either way."},
             {"question": "My account is locked",
              "answer": "Five failed attempts locks the account for thirty minutes as a guard; after that the reset link clears it. Locked longer than an hour becomes a case we investigate."},
         ]},
    ],
    "workflows": [
        {"name": "Ticket opener",
         "description": "sms.received -> shape -> Support tickets -> business_start: "
                        "every inbound text opens a ticket row AND a tracked case "
                        "(ref = the sender's phone) - the machine watches it from "
                        "day one and the door inherits the SLA.",
         "trigger": {"type": "event_trigger", "params": {"event_type": "sms.received"}},
         "steps": [
             {"type": "python_transform", "name": "Shape the ticket",
              "params": {"code": ("r = df.iloc[0] if len(df) else {}\n"
                                  "pl = r.get('payload') or {}\n"
                                  "result = [{'ticket': '', 'requester': r.get('actor', ''), "
                                  "'phone': r.get('actor', ''), "
                                  "'subject': (pl.get('text') or '')[:60], "
                                  "'status': 'opened'}]")}},
             {"type": "dataset_write", "name": "Open the ticket row",
              "params": {"dataset": "Support tickets", "mode": "append"}},
             {"type": "business_start", "name": "Track the case",
              "params": {"process": "Case lifecycle",
                         "ref": "{{ nodes.n_trigger.output.actor }}",
                         "title": "Inbound from {{ nodes.n_trigger.output.actor }}",
                         "actor": "support-operator",
                         "due_in_seconds": 86400,
                         "on_duplicate": "skip"}},
         ]},
        {"name": "Case logger",
         "description": "call.ended -> shape -> Ticket events. Every call on the line "
                        "leaves evidence - the desk sees the traffic behind the cases.",
         "trigger": {"type": "event_trigger", "params": {"event_type": "call.ended"}},
         "steps": [
             {"type": "python_transform", "name": "Shape the touch",
              "params": {"code": ("r = df.iloc[0] if len(df) else {}\n"
                                  "pl = r.get('payload') or {}\n"
                                  "result = [{'session_id': r.get('session_id', ''), "
                                  "'channel': r.get('source', 'voice'), "
                                  "'action': 'call ' + str(pl.get('end_reason', 'ended')), "
                                  "'at': r.get('triggered_at', '')}]")}},
             {"type": "dataset_write", "name": "Append the touch",
              "params": {"dataset": "Ticket events", "mode": "append"}},
         ]},
        {"name": "Case advancer",
         "description": "call.ended -> business_advance: a completed call from a "
                        "tracked requester moves their case (opened -> assigned). "
                        "Callers the desk does not track, and cases already past "
                        "the move, skip honestly - the call is evidence, not a "
                        "forced move.",
         "trigger": {"type": "event_trigger", "params": {"event_type": "call.ended"}},
         "steps": [
             {"type": "business_advance", "name": "Move the caller's case",
              "params": {"process": "Case lifecycle", "ref": "{{ input.actor }}",
                         "transition": "triage", "actor": "support-operator",
                         "note": "a real call ended - the case moves itself",
                         "on_missing": "skip", "on_refusal": "skip"}},
         ]},
    ],
    "agent": {"name": "Support agent",
              "greeting": "Support, good day - what are we solving?",
              "system_prompt": ("You are a patient, precise support agent. Answer from the "
                                "knowledge matches in metadata.knowledge; when the answer is "
                                "not there or the caller asks for a human, take the case and "
                                "promise the callback - never guess."),
              "knowledge": {"dataset": "Support FAQ", "text_column": "question",
                            "answer_column": "answer", "top_k": 1}},
    "rooms": [{"name": "Support desk room", "title": "Support desk room",
               "modality": "audio"}],
    "queues": [{"name": "Support help queue", "room": "Support desk room",
                "bind_agent": True,
                "config": {"max_size": 40, "max_wait_seconds": 300,
                           "announce": {"enabled": True, "interval_seconds": 60},
                           "sms": {"enabled": True, "channel_id": "", "template": ""},
                           "callback": {"enabled": True, "endpoint_id": ""}}}],
    "campaign": None,
    "processes": [
        {"name": "Case lifecycle",
         "description": ("The case journey as a state machine - one tracked instance "
                         "per case remembering status and context across days; calls "
                         "move it, inbound texts open it, the escalation door nudges "
                         "it when it goes stale."),
         "definition": {
             "states": ["opened", "assigned", "investigating", "waiting",
                        "resolved", "closed"],
             "initial": "opened",
             "transitions": [
                 {"name": "triage", "from": "opened", "to": "assigned"},
                 {"name": "investigate", "from": "assigned", "to": "investigating"},
                 {"name": "wait_on_caller", "from": "investigating", "to": "waiting"},
                 {"name": "resume", "from": "waiting", "to": "investigating"},
                 {"name": "resolve", "from": "investigating", "to": "resolved"},
                 {"name": "close", "from": "resolved", "to": "closed"},
                 {"name": "reopen", "from": "resolved", "to": "assigned"},
                 # the escalation door's move: a stale case re-enters its
                 # state (a fresh stint, on the record) so the desk sees
                 # the nudge - a case can go stale at ANY working stage
                 {"name": "escalate", "from": "opened", "to": "opened",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "assigned", "to": "assigned",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "investigating", "to": "investigating",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "waiting", "to": "waiting",
                  "description": "SLA breach nudge - the door's move"},
             ],
         },
         "seed_from_dataset": "Support tickets",
         "ref_column": "phone",
         "title_from": ["ticket", "subject"],
         "state_column": "status",
         "due_in_seconds": 24 * 3600,
        },
    ],
    "dashboard": {"name": "Support Operator Board",
                  "description": "The desk at a glance - open cases, inbound traffic, "
                                 "case pressure by status."},
    "notes": [
        "The help queue announces positions, texts waiting callers over the SMS "
        "backchannel (bind config.sms.channel_id when the credentials exist) and "
        "offers callbacks - the desk works the same either way.",
        "The opener opens BOTH the ticket row and a tracked case (ref = the "
        "sender's phone, on_duplicate=skip): a tracked phone texts again and "
        "nothing doubles. It installs INACTIVE - boot the system (start with "
        "activate_workflows) to open the reactive path.",
        "Stale cases: the machine defines escalate self-loops on opened / "
        "assigned / investigating / waiting - the scheduler door (POST "
        "/scheduler/escalations/tick) walks past-SLA cases through them.",
        "Email/WhatsApp/Telegram channels are the installer's endpoints to bind "
        "(channels page) - the operator is the system underneath them, channels "
        "stay interchangeable.",
    ],
}

_OPERATIONS_OPERATOR = {
    "slug": "operations-operator",
    "name": "Operations Operator",
    "tagline": ("The back office in one click: internal requests that intake "
                "themselves and land tracked in the approval machine, an ops "
                "concierge grounded in the handbook, the work queue, and the "
                "staff board."),
    "category": "Operations",
    "icon": "clipboard-list",
    "color": "#a78bfa",
    "outcomes": [
        "Ops requests dataset (requests + statuses)",
        "Request lifecycle business process (pre-wired, seeded from the requests)",
        "Customer onboarding machine - the leg a won deal opens (cross-operator journeys)",
        "Ops concierge grounded in the handbook",
        "Work queue (announce + SMS + callback wired)",
        "sms.received -> request intake (event-reactive: row AND tracked request)",
        "call.ended -> decision logger + approval advancer (event-reactive)",
        "Ops approvals dataset (every decision)",
        "Staff dashboard over the back office",
    ],
    "datasets": [
        {"name": "Ops requests",
         "description": "The work queue's spine - one row per internal request; "
                        "the intake workflow appends inbound asks, the team works "
                        "the statuses",
         "columns": ["request", "kind", "requester", "phone", "status"],
         "rows": [
             {"request": "laptop replacement - MRI-214", "kind": "equipment",
              "requester": "Ruth Bello", "phone": "+15550004111", "status": "in_review"},
             {"request": "standing desk", "kind": "equipment",
              "requester": "Kofi Adjei", "phone": "+15550004222", "status": "approved"},
             {"request": "badge reissue", "kind": "facilities",
              "requester": "Lena Muller", "phone": "+15550004333", "status": "submitted"},
         ]},
        {"name": "Ops approvals",
         "description": "One row per decision (the logger appends every approval "
                        "call) - who decided what, and when",
         "columns": ["request_ref", "decision", "decided_by", "at"],
         "rows": []},
        {"name": "Onboarding cases",
         "description": "The customer onboarding desk's queue - one row per new "
                        "customer being brought live; rows land here when a deal "
                        "WINS (the cross-operator journey from the Sales lead "
                        "pipeline) or through a bulk import, and the machine "
                        "tracks each one to handoff",
         "columns": ["customer", "phone", "plan", "stage"],
         "rows": [
             {"customer": "Northwind Traders", "phone": "+15550007101",
              "plan": "growth", "stage": "kickoff"},
             {"customer": "Acme Corp", "phone": "+15550007202",
              "plan": "enterprise", "stage": "training"},
         ]},
        {"name": "Ops handbook",
         "description": "The concierge's knowledge - how the back office runs",
         "columns": ["question", "answer"],
         "rows": [
             {"question": "How do I file a request",
              "answer": "Text this line with what you need - the request lands tracked immediately - or ask the concierge to file it with you."},
             {"question": "How long does a request take",
              "answer": "The SLA is two working days in review; if it sits longer the escalation door nudges the owner automatically."},
             {"question": "Who approves equipment",
              "answer": "Equipment goes to your line manager; anything above the limit in the policy adds finance as a second approval."},
             {"question": "What is the expense limit",
              "answer": "Requests up to five hundred need one approval; above that a second approver joins and the machine waits for both."},
             {"question": "How do I mark a request urgent",
              "answer": "Include the word urgent and the reason - urgent requests page the on-call coordinator through the work queue."},
         ]},
    ],
    "workflows": [
        {"name": "Ops request intake",
         "description": "sms.received -> shape -> Ops requests -> business_start: "
                        "every inbound text opens a request row AND a tracked "
                        "instance (ref = the sender's phone) - the approval "
                        "machine watches it from day one.",
         "trigger": {"type": "event_trigger", "params": {"event_type": "sms.received"}},
         "steps": [
             {"type": "python_transform", "name": "Shape the request",
              "params": {"code": ("r = df.iloc[0] if len(df) else {}\n"
                                  "pl = r.get('payload') or {}\n"
                                  "result = [{'request': (pl.get('text') or '')[:60], "
                                  "'kind': '', 'requester': r.get('actor', ''), "
                                  "'phone': r.get('actor', ''), 'status': 'submitted'}]")}},
             {"type": "dataset_write", "name": "Open the request row",
              "params": {"dataset": "Ops requests", "mode": "append"}},
             {"type": "business_start", "name": "Track the request",
              "params": {"process": "Request lifecycle",
                         "ref": "{{ nodes.n_trigger.output.actor }}",
                         "title": "Request from {{ nodes.n_trigger.output.actor }}",
                         "actor": "operations-operator",
                         "due_in_seconds": 172800,
                         "on_duplicate": "skip"}},
         ]},
        {"name": "Ops decision logger",
         "description": "call.ended -> shape -> Ops approvals. Approval calls land "
                        "on the record - the trail behind every decision.",
         "trigger": {"type": "event_trigger", "params": {"event_type": "call.ended"}},
         "steps": [
             {"type": "python_transform", "name": "Shape the decision",
              "params": {"code": ("r = df.iloc[0] if len(df) else {}\n"
                                  "pl = r.get('payload') or {}\n"
                                  "result = [{'request_ref': '', "
                                  "'decision': 'call ' + str(pl.get('end_reason', 'ended')), "
                                  "'decided_by': r.get('actor', ''), "
                                  "'at': r.get('triggered_at', '')}]")}},
             {"type": "dataset_write", "name": "Append the decision",
              "params": {"dataset": "Ops approvals", "mode": "append"}},
         ]},
        {"name": "Approval advancer",
         "description": "call.ended -> business_advance: a completed call from a "
                        "tracked requester moves their request (submitted -> "
                        "in_review). Unknown callers and requests already past the "
                        "move skip honestly.",
         "trigger": {"type": "event_trigger", "params": {"event_type": "call.ended"}},
         "steps": [
             {"type": "business_advance", "name": "Move the requester's request",
              "params": {"process": "Request lifecycle", "ref": "{{ input.actor }}",
                         "transition": "review", "actor": "operations-operator",
                         "note": "a real call ended - the request moves itself",
                         "on_missing": "skip", "on_refusal": "skip"}},
         ]},
    ],
    "agent": {"name": "Ops concierge",
              "greeting": "Operations here - what do you need moved?",
              "system_prompt": ("You are the internal operations concierge. Answer from the "
                                "knowledge matches in metadata.knowledge; help employees file "
                                "requests and check statuses; never promise an approval - "
                                "the machine and the approvers decide."),
              "knowledge": {"dataset": "Ops handbook", "text_column": "question",
                            "answer_column": "answer", "top_k": 1}},
    "rooms": [{"name": "Ops war room", "title": "Ops war room", "modality": "audio"}],
    "queues": [{"name": "Ops work queue", "room": "Ops war room",
                "bind_agent": True,
                "config": {"max_size": 30, "max_wait_seconds": 300,
                           "announce": {"enabled": True, "interval_seconds": 90},
                           "sms": {"enabled": True, "channel_id": "", "template": ""},
                           "callback": {"enabled": True, "endpoint_id": ""}}}],
    "campaign": None,
    "processes": [
        {"name": "Request lifecycle",
         "description": ("The approval machine - one tracked instance per request "
                         "remembering where it stands across days; inbound texts "
                         "open it, calls move it, the door nudges it when the SLA "
                         "slips."),
         "definition": {
             "states": ["submitted", "in_review", "approved", "in_progress",
                        "done", "rejected"],
             "initial": "submitted",
             "transitions": [
                 {"name": "review", "from": "submitted", "to": "in_review"},
                 {"name": "approve", "from": "in_review", "to": "approved"},
                 {"name": "reject", "from": "in_review", "to": "rejected"},
                 {"name": "start", "from": "approved", "to": "in_progress"},
                 {"name": "complete", "from": "in_progress", "to": "done"},
                 {"name": "escalate", "from": "submitted", "to": "submitted",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "in_review", "to": "in_review",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "approved", "to": "approved",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "in_progress", "to": "in_progress",
                  "description": "SLA breach nudge - the door's move"},
             ],
         },
         "seed_from_dataset": "Ops requests",
         "ref_column": "phone",
         "title_from": ["request", "requester"],
         "state_column": "status",
         "due_in_seconds": 2 * 24 * 3600,
        },
        {"name": "Customer onboarding",
         "description": ("The won deal's landing path as a state machine - one "
                         "tracked instance per new customer. Cases OPEN "
                         "THEMSELVES when a deal wins (the cross-operator "
                         "journey from the Sales operator's lead pipeline: "
                         "won -> kickoff), and the team walks them to "
                         "handoff; the door nudges a stalled onboarding."),
         "definition": {
             "states": ["kickoff", "provisioning", "training", "go_live",
                        "handed_off"],
             "initial": "kickoff",
             "transitions": [
                 {"name": "provision", "from": "kickoff", "to": "provisioning"},
                 {"name": "train", "from": "provisioning", "to": "training"},
                 {"name": "go_live", "from": "training", "to": "go_live"},
                 {"name": "hand_off", "from": "go_live", "to": "handed_off"},
                 {"name": "escalate", "from": "kickoff", "to": "kickoff",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "provisioning", "to": "provisioning",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "training", "to": "training",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "go_live", "to": "go_live",
                  "description": "SLA breach nudge - the door's move"},
             ],
         },
         "seed_from_dataset": "Onboarding cases",
         "ref_column": "phone",
         "title_from": ["customer", "plan"],
         "state_column": "stage",
         "due_in_seconds": 14 * 24 * 3600,
        },
    ],
    "dashboard": {"name": "Operations Operator Board",
                  "description": "The back office at a glance - requests by status, "
                                 "approval traffic, SLA pressure."},
    "notes": [
        "The intake opens BOTH the request row and a tracked instance (ref = "
        "the sender's phone, on_duplicate=skip) - a requester texting again "
        "never doubles. It installs INACTIVE - boot the system to open the "
        "reactive path.",
        "The machine defines escalate self-loops on every working state - the "
        "scheduler door sweeps past-SLA requests through them (POST "
        "/scheduler/escalations/tick).",
        "Cross-operator journeys land here: the Sales operator's lead pipeline "
        "fires a journey on 'won' that opens a Customer onboarding case at "
        "kickoff (ref = the lead's phone), and the machine fires its own leg "
        "on 'handed_off' - the Finance operator's invoice lifecycle opens the "
        "billing instance by itself. Install all three and the handoff across "
        "the departments is a chain of state changes, not meetings.",
        "The handbook is the concierge's knowledge - edit the dataset and the "
        "answers follow, no redeploy.",
    ],
}

_HR_OPERATOR = {
    "slug": "hr-operator",
    "name": "HR Operator",
    "tagline": ("People ops in one click: employee records, leave requests that "
                "move through the machine by themselves, onboarding pipelines "
                "that remember where every hire stands, an HR assistant grounded "
                "in the handbook, and the staff board."),
    "category": "People",
    "icon": "users",
    "color": "#fbbf24",
    "outcomes": [
        "People dataset (employee records + onboarding stages)",
        "Onboarding pipeline business process (pre-wired, seeded from the people)",
        "Leave requests dataset + Leave pipeline business process (seeded)",
        "HR assistant grounded in the handbook",
        "HR front desk queue (announce + SMS + callback wired)",
        "sms.received -> leave intake (event-reactive: row AND tracked request)",
        "call.ended -> onboarding advancer (event-reactive)",
        "Staff dashboard over the people",
    ],
    "datasets": [
        {"name": "People",
         "description": "The employee records - one row per person; the onboarding "
                        "column is the stage the machine tracks",
         "columns": ["employee", "role", "phone", "stage"],
         "rows": [
             {"employee": "Ada Nwosu", "role": "data engineer",
              "phone": "+15550005111", "stage": "day_one"},
             {"employee": "Tom Weber", "role": "account executive",
              "phone": "+15550005222", "stage": "accepted"},
             {"employee": "Sara Haddad", "role": "support lead",
              "phone": "+15550005333", "stage": "buddied"},
         ]},
        {"name": "Leave requests",
         "description": "One row per leave ask - the intake workflow appends "
                        "inbound requests, the pipeline tracks them",
         "columns": ["employee", "phone", "kind", "dates", "status"],
         "rows": [
             {"employee": "Ada Nwosu", "phone": "+15550005111", "kind": "annual",
              "dates": "2026-03-02..03-06", "status": "manager_review"},
             {"employee": "Tom Weber", "phone": "+15550005222", "kind": "sick",
              "dates": "2026-01-12", "status": "approved"},
             {"employee": "Sara Haddad", "phone": "+15550005333", "kind": "parental",
              "dates": "from 2026-04-01", "status": "requested"},
         ]},
        {"name": "HR handbook",
         "description": "The assistant's knowledge - policy answers, not rumors",
         "columns": ["question", "answer"],
         "rows": [
             {"question": "How much annual leave do I have",
              "answer": "The standard allowance is twenty-five days a year plus public holidays; your balance and carried-over days are on the people record."},
             {"question": "How far ahead must I request leave",
              "answer": "Two weeks for single days, four weeks for a week or more - the pipeline nudges the manager if the review sits past the SLA."},
             {"question": "What are the public holidays",
              "answer": "The holiday calendar is published each December; days that fall on weekends roll to the next working day."},
             {"question": "When is payroll",
              "answer": "Payroll lands on the last working day of the month; expense claims close five days before for the same run."},
             {"question": "What happens on day one",
              "answer": "Badge and accounts at nine, the buddy introduction at ten, the team lunch at noon - the onboarding pipeline tracks each step."},
         ]},
    ],
    "workflows": [
        {"name": "Leave intake",
         "description": "sms.received -> shape -> Leave requests -> business_start: "
                        "every inbound text opens a leave row AND a tracked "
                        "request (ref = the sender's phone) - the leave pipeline "
                        "watches it from day one.",
         "trigger": {"type": "event_trigger", "params": {"event_type": "sms.received"}},
         "steps": [
             {"type": "python_transform", "name": "Shape the request",
              "params": {"code": ("r = df.iloc[0] if len(df) else {}\n"
                                  "pl = r.get('payload') or {}\n"
                                  "result = [{'employee': '', 'phone': r.get('actor', ''), "
                                  "'kind': (pl.get('text') or '')[:20], "
                                  "'dates': (pl.get('text') or '')[20:60], "
                                  "'status': 'requested'}]")}},
             {"type": "dataset_write", "name": "Open the leave row",
              "params": {"dataset": "Leave requests", "mode": "append"}},
             {"type": "business_start", "name": "Track the leave request",
              "params": {"process": "Leave pipeline",
                         "ref": "{{ nodes.n_trigger.output.actor }}",
                         "title": "Leave ask from {{ nodes.n_trigger.output.actor }}",
                         "actor": "hr-operator",
                         "due_in_seconds": 259200,
                         "on_duplicate": "skip"}},
         ]},
        {"name": "Onboarding advancer",
         "description": "call.ended -> business_advance: a completed check-in call "
                        "with a tracked hire moves their onboarding (day_one -> "
                        "buddied). Hires the machine does not track, and stages "
                        "already past, skip honestly.",
         "trigger": {"type": "event_trigger", "params": {"event_type": "call.ended"}},
         "steps": [
             {"type": "business_advance", "name": "Move the hire's onboarding",
              "params": {"process": "Onboarding pipeline",
                         "ref": "{{ input.actor }}",
                         "transition": "check_in", "actor": "hr-operator",
                         "note": "a real check-in call ended - onboarding moves itself",
                         "on_missing": "skip", "on_refusal": "skip"}},
         ]},
    ],
    "agent": {"name": "HR assistant",
              "greeting": "People ops, good day - how can I help?",
              "system_prompt": ("You are a warm, discreet HR assistant. Answer from the "
                                "knowledge matches in metadata.knowledge; log leave asks; "
                                "for anything sensitive offer a human callback - never "
                                "improvise policy."),
              "knowledge": {"dataset": "HR handbook", "text_column": "question",
                            "answer_column": "answer", "top_k": 1}},
    "rooms": [{"name": "HR interview room", "title": "HR interview room",
               "modality": "audio"}],
    "queues": [{"name": "HR front desk", "room": "HR interview room",
                "bind_agent": True,
                "config": {"max_size": 20, "max_wait_seconds": 300,
                           "announce": {"enabled": True, "interval_seconds": 90},
                           "sms": {"enabled": True, "channel_id": "", "template": ""},
                           "callback": {"enabled": True, "endpoint_id": ""}}}],
    "campaign": None,
    "processes": [
        {"name": "Onboarding pipeline",
         "description": ("Every hire's first weeks as a state machine - one tracked "
                         "instance per person remembering the stage across weeks; "
                         "check-in calls move it, the door nudges it when a hire "
                         "sits stuck."),
         "definition": {
             "states": ["offer_sent", "accepted", "day_one", "buddied", "done"],
             "initial": "offer_sent",
             "transitions": [
                 {"name": "accept", "from": "offer_sent", "to": "accepted"},
                 {"name": "start", "from": "accepted", "to": "day_one"},
                 {"name": "check_in", "from": "day_one", "to": "buddied"},
                 {"name": "finish", "from": "buddied", "to": "done"},
                 {"name": "escalate", "from": "offer_sent", "to": "offer_sent",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "accepted", "to": "accepted",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "day_one", "to": "day_one",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "buddied", "to": "buddied",
                  "description": "SLA breach nudge - the door's move"},
             ],
         },
         "seed_from_dataset": "People",
         "ref_column": "phone",
         "title_from": ["employee", "role"],
         "state_column": "stage",
         "due_in_seconds": 14 * 24 * 3600,
        },
        {"name": "Leave pipeline",
         "description": ("The leave ask as a state machine - one tracked instance "
                         "per request; inbound texts open it, managers move it, "
                         "the door nudges a review that sits past its SLA."),
         "definition": {
             "states": ["requested", "manager_review", "approved", "scheduled",
                        "taken", "closed", "declined"],
             "initial": "requested",
             "transitions": [
                 {"name": "review", "from": "requested", "to": "manager_review"},
                 {"name": "approve", "from": "manager_review", "to": "approved"},
                 {"name": "decline", "from": "manager_review", "to": "declined"},
                 {"name": "schedule", "from": "approved", "to": "scheduled"},
                 {"name": "mark_taken", "from": "scheduled", "to": "taken"},
                 {"name": "close", "from": "taken", "to": "closed"},
                 {"name": "escalate", "from": "requested", "to": "requested",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "manager_review", "to": "manager_review",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "approved", "to": "approved",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "scheduled", "to": "scheduled",
                  "description": "SLA breach nudge - the door's move"},
             ],
         },
         "seed_from_dataset": "Leave requests",
         "ref_column": "phone",
         "title_from": ["employee", "kind"],
         "state_column": "status",
         "due_in_seconds": 3 * 24 * 3600,
        },
    ],
    "dashboard": {"name": "HR Operator Board",
                  "description": "The people at a glance - onboarding stages, leave "
                                 "pipeline, who sits where."},
    "notes": [
        "TWO machines ship bound: the onboarding pipeline seeded from People "
        "(one instance per employee at its stage) and the leave pipeline seeded "
        "from Leave requests. Both arrive pre-wired - calls move onboarding, "
        "texts open leave.",
        "The intake opens BOTH the leave row and a tracked request (ref = the "
        "sender's phone, on_duplicate=skip). It installs INACTIVE - boot the "
        "system to open the reactive path.",
        "Both machines define escalate self-loops on their working states - the "
        "scheduler door sweeps past-SLA hires and leave asks through them (POST "
        "/scheduler/escalations/tick).",
        "The handbook is the assistant's knowledge - edit the dataset and the "
        "answers follow, no redeploy.",
    ],
}

_FINANCE_OPERATOR = {
    "slug": "finance-operator",
    "name": "Finance Operator",
    "tagline": ("Money ops in one click: invoices that intake themselves and land "
                "tracked in the approval machine, a collections dialer ready for "
                "your endpoint, a finance clerk grounded in the policy, and the "
                "staff board."),
    "category": "Finance",
    "icon": "banknote",
    "color": "#2dd4bf",
    "outcomes": [
        "Invoices dataset (payables + statuses)",
        "Invoice lifecycle business process (pre-wired, seeded from the invoices)",
        "The chains' landing leg - onboarding handoffs, delivered orders and "
        "billed visits open their invoices here (cross-operator journeys)",
        "Finance clerk grounded in the finance policy",
        "Approvals queue (announce + SMS + callback wired)",
        "Collections campaign (empty dialer, targets from the invoices)",
        "sms.received -> invoice intake (event-reactive: row AND tracked invoice)",
        "call.ended -> ledger logger + payment advancer (event-reactive)",
        "Ledger events dataset (every touch)",
        "Staff dashboard over the money",
    ],
    "datasets": [
        {"name": "Invoices",
         "description": "The payables ledger - one row per invoice; the intake "
                        "workflow appends inbound bills, the team works the "
                        "statuses",
         "columns": ["invoice", "vendor", "phone", "amount", "status"],
         "rows": [
             {"invoice": "INV-2041", "vendor": "Brightline Media",
              "phone": "+15550006111", "amount": "1250.00", "status": "matched"},
             {"invoice": "INV-2038", "vendor": "Corelink Supplies",
              "phone": "+15550006222", "amount": "480.00", "status": "approved"},
             {"invoice": "INV-2035", "vendor": "Datahost",
              "phone": "+15550006333", "amount": "990.00", "status": "received"},
         ]},
        {"name": "Ledger events",
         "description": "One row per touch (the logger appends every call) - the "
                        "trail behind the numbers",
         "columns": ["invoice_ref", "action", "at"],
         "rows": []},
        {"name": "Finance policy",
         "description": "The clerk's knowledge - the rules the money follows",
         "columns": ["question", "answer"],
         "rows": [
             {"question": "What is the approval threshold",
              "answer": "Invoices up to five hundred are approved on the clerk's match alone; above that a second approver joins before the payment run."},
             {"question": "When is the payment run",
              "answer": "Payments go out every Friday; invoices approved by Thursday evening make that week's run."},
             {"question": "What is three-way matching",
              "answer": "Invoice against purchase order against goods received - all three agree and the machine moves the invoice to matched."},
             {"question": "How do I dispute an invoice",
              "answer": "Move the case to disputed with the reason; the vendor gets the callback and the disputed trail stays on the ledger."},
             {"question": "What happens on overdue collections",
              "answer": "The collections campaign dials the contact numbers with the clerk's script; promises to pay are logged and the machine tracks them."},
         ]},
    ],
    "workflows": [
        {"name": "Invoice intake",
         "description": "sms.received -> shape -> Invoices -> business_start: every "
                        "inbound bill opens an invoice row AND a tracked instance "
                        "(ref = the vendor's phone) - the machine watches it from "
                        "day one.",
         "trigger": {"type": "event_trigger", "params": {"event_type": "sms.received"}},
         "steps": [
             {"type": "python_transform", "name": "Shape the invoice",
              "params": {"code": ("r = df.iloc[0] if len(df) else {}\n"
                                  "pl = r.get('payload') or {}\n"
                                  "result = [{'invoice': '', "
                                  "'vendor': (pl.get('text') or '')[:40], "
                                  "'phone': r.get('actor', ''), 'amount': '', "
                                  "'status': 'received'}]")}},
             {"type": "dataset_write", "name": "Open the invoice row",
              "params": {"dataset": "Invoices", "mode": "append"}},
             {"type": "business_start", "name": "Track the invoice",
              "params": {"process": "Invoice lifecycle",
                         "ref": "{{ nodes.n_trigger.output.actor }}",
                         "title": "Invoice from {{ nodes.n_trigger.output.actor }}",
                         "actor": "finance-operator",
                         "due_in_seconds": 432000,
                         "on_duplicate": "skip"}},
         ]},
        {"name": "Ledger logger",
         "description": "call.ended -> shape -> Ledger events. Vendor and collections "
                        "calls land on the ledger - the trail behind the numbers.",
         "trigger": {"type": "event_trigger", "params": {"event_type": "call.ended"}},
         "steps": [
             {"type": "python_transform", "name": "Shape the touch",
              "params": {"code": ("r = df.iloc[0] if len(df) else {}\n"
                                  "pl = r.get('payload') or {}\n"
                                  "result = [{'invoice_ref': '', "
                                  "'action': 'call ' + str(pl.get('end_reason', 'ended')), "
                                  "'at': r.get('triggered_at', '')}]")}},
             {"type": "dataset_write", "name": "Append the ledger touch",
              "params": {"dataset": "Ledger events", "mode": "append"}},
         ]},
        {"name": "Payment advancer",
         "description": "call.ended -> business_advance: a completed call from a "
                        "tracked vendor moves their invoice (matched -> approved). "
                        "Vendors the machine does not track, and invoices already "
                        "past the move, skip honestly.",
         "trigger": {"type": "event_trigger", "params": {"event_type": "call.ended"}},
         "steps": [
             {"type": "business_advance", "name": "Move the vendor's invoice",
              "params": {"process": "Invoice lifecycle", "ref": "{{ input.actor }}",
                         "transition": "approve", "actor": "finance-operator",
                         "note": "a real call ended - the invoice moves itself",
                         "on_missing": "skip", "on_refusal": "skip"}},
         ]},
    ],
    "agent": {"name": "Finance clerk",
              "greeting": "Finance here - which invoice can we settle?",
              "system_prompt": ("You are a careful finance clerk. Answer from the knowledge "
                                "matches in metadata.knowledge; never confirm a payment that "
                                "the machine has not approved - walk the caller through the "
                                "lifecycle instead."),
              "knowledge": {"dataset": "Finance policy", "text_column": "question",
                            "answer_column": "answer", "top_k": 1}},
    "rooms": [{"name": "Finance review room", "title": "Finance review room",
               "modality": "audio"}],
    "queues": [{"name": "Finance approvals queue", "room": "Finance review room",
                "bind_agent": True,
                "config": {"max_size": 20, "max_wait_seconds": 300,
                           "announce": {"enabled": True, "interval_seconds": 90},
                           "sms": {"enabled": True, "channel_id": "", "template": ""},
                           "callback": {"enabled": True, "endpoint_id": ""}}}],
    "campaign": {"name": "Collections campaign",
                 "config": {}},
    "processes": [
        {"name": "Invoice lifecycle",
         "description": ("The payable as a state machine - one tracked instance per "
                         "invoice remembering where it stands across weeks; vendor "
                         "calls move it, inbound bills open it, the door nudges an "
                         "approval that sits past its SLA."),
         "definition": {
             "states": ["received", "matched", "approved", "scheduled", "paid",
                        "disputed"],
             "initial": "received",
             "transitions": [
                 {"name": "match", "from": "received", "to": "matched"},
                 {"name": "approve", "from": "matched", "to": "approved"},
                 {"name": "dispute", "from": "matched", "to": "disputed"},
                 {"name": "reinstate", "from": "disputed", "to": "matched"},
                 {"name": "schedule", "from": "approved", "to": "scheduled"},
                 {"name": "pay", "from": "scheduled", "to": "paid"},
                 {"name": "escalate", "from": "received", "to": "received",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "matched", "to": "matched",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "approved", "to": "approved",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "scheduled", "to": "scheduled",
                  "description": "SLA breach nudge - the door's move"},
             ],
         },
         "seed_from_dataset": "Invoices",
         "ref_column": "phone",
         "title_from": ["invoice", "vendor"],
         "state_column": "status",
         "due_in_seconds": 5 * 24 * 3600,
        },
    ],
    "dashboard": {"name": "Finance Operator Board",
                  "description": "The money at a glance - invoices by status, ledger "
                                 "traffic, approval pressure."},
    "notes": [
        "The collections campaign installs EMPTY on purpose: add targets from the "
        "invoices (POST /voice/campaigns/{id}/targets) and bind a telnyx voice "
        "endpoint when the dialing credentials exist - until then dials skip "
        "honestly.",
        "The intake opens BOTH the invoice row and a tracked instance (ref = the "
        "vendor's phone, on_duplicate=skip). It installs INACTIVE - boot the "
        "system to open the reactive path.",
        "The machine defines escalate self-loops on every working state - the "
        "scheduler door sweeps past-SLA invoices through them (POST "
        "/scheduler/escalations/tick).",
        "The policy is the flagship DIGEST: one summary per day instead of N "
        "knocks per invoice - bind escalation_policy.to + a channel endpoint "
        "and the daily summary lands in the inbox.",
        "Cross-operator journeys land here: a handed-off onboarding (Operations), "
        "a delivered order (Logistics) and a billed visit (Clinic) each open an "
        "invoice at 'received' with their own SLA - the payable machine never "
        "waits for someone to type the bill.",
        "The policy dataset is the clerk's knowledge - edit it and the answers "
        "follow, no redeploy.",
    ],
}

_PROCUREMENT_OPERATOR = {
    "slug": "procurement-operator",
    "name": "Procurement Operator",
    "tagline": ("Buying ops in one click: purchase requests that intake themselves "
                "and land tracked in the quote-approve-order machine, a "
                "procurement assistant grounded in the policy, the vendor desk, "
                "and the staff board."),
    "category": "Procurement",
    "icon": "shopping-cart",
    "color": "#fb923c",
    "outcomes": [
        "Purchase requests dataset (asks + statuses)",
        "Purchase lifecycle business process (pre-wired, seeded from the requests)",
        "Procurement assistant grounded in the policy",
        "Vendor desk queue (announce + SMS + callback wired)",
        "sms.received -> purchase intake (event-reactive: row AND tracked request)",
        "call.ended -> quote logger + PO advancer (event-reactive)",
        "Vendor quotes dataset (the market view)",
        "Staff dashboard over the buying",
    ],
    "datasets": [
        {"name": "Purchase requests",
         "description": "The buying desk - one row per purchase request; the intake "
                        "workflow appends inbound asks, the team works the "
                        "statuses",
         "columns": ["request", "item", "vendor", "phone", "status"],
         "rows": [
             {"request": "PR-301", "item": "24 monitors", "vendor": "DeskWorks",
              "phone": "+15550007111", "status": "quoted"},
             {"request": "PR-302", "item": "office chairs x6", "vendor": "SeatCo",
              "phone": "+15550007222", "status": "approved"},
             {"request": "PR-303", "item": "CRM licenses x10", "vendor": "Softline",
              "phone": "+15550007333", "status": "requested"},
         ]},
        {"name": "Vendor quotes",
         "description": "One row per quote conversation (the logger appends every "
                        "vendor call) - the market view behind each purchase",
         "columns": ["request_ref", "vendor", "phone", "amount",
                     "lead_time_days", "at"],
         "rows": []},
        {"name": "Procurement policy",
         "description": "The assistant's knowledge - the rules the buying follows",
         "columns": ["question", "answer"],
         "rows": [
             {"question": "What is the three-quote rule",
              "answer": "Purchases above the limit need three written quotes; the quotes dataset holds the market view and the machine holds the request until they land."},
             {"question": "What is the approval limit",
              "answer": "Requests up to one thousand are approved on the desk's judgement; above that the budget owner approves inside the machine."},
             {"question": "Who are the preferred vendors",
              "answer": "Preferred vendors ship in the policy dataset with agreed terms; buying outside them needs a written reason on the request."},
             {"question": "How do deliveries get received",
              "answer": "The machine moves the purchase to receiving when the order ships; receiving confirms the count and the request closes only when the goods are booked."},
             {"question": "What about returns",
              "answer": "Rejected quotes end the request honestly; wrong goods return through the same machine - the vendor call opens the exception."},
         ]},
    ],
    "workflows": [
        {"name": "Purchase intake",
         "description": "sms.received -> shape -> Purchase requests -> "
                        "business_start: every inbound ask opens a request row AND "
                        "a tracked instance (ref = the requester's phone) - the "
                        "machine watches it from day one.",
         "trigger": {"type": "event_trigger", "params": {"event_type": "sms.received"}},
         "steps": [
             {"type": "python_transform", "name": "Shape the request",
              "params": {"code": ("r = df.iloc[0] if len(df) else {}\n"
                                  "pl = r.get('payload') or {}\n"
                                  "result = [{'request': (pl.get('text') or '')[:20], "
                                  "'item': (pl.get('text') or '')[20:60], "
                                  "'vendor': '', 'phone': r.get('actor', ''), "
                                  "'status': 'requested'}]")}},
             {"type": "dataset_write", "name": "Open the request row",
              "params": {"dataset": "Purchase requests", "mode": "append"}},
             {"type": "business_start", "name": "Track the purchase",
              "params": {"process": "Purchase lifecycle",
                         "ref": "{{ nodes.n_trigger.output.actor }}",
                         "title": "Purchase ask from {{ nodes.n_trigger.output.actor }}",
                         "actor": "procurement-operator",
                         "due_in_seconds": 432000,
                         "on_duplicate": "skip"}},
         ]},
        {"name": "Quote logger",
         "description": "call.ended -> shape -> Vendor quotes. Vendor calls land on "
                        "the market view - the quotes behind each purchase.",
         "trigger": {"type": "event_trigger", "params": {"event_type": "call.ended"}},
         "steps": [
             {"type": "python_transform", "name": "Shape the quote touch",
              "params": {"code": ("r = df.iloc[0] if len(df) else {}\n"
                                  "pl = r.get('payload') or {}\n"
                                  "result = [{'request_ref': '', "
                                  "'vendor': r.get('actor', ''), "
                                  "'phone': r.get('actor', ''), 'amount': '', "
                                  "'lead_time_days': '', "
                                  "'at': r.get('triggered_at', '')}]")}},
             {"type": "dataset_write", "name": "Append the quote touch",
              "params": {"dataset": "Vendor quotes", "mode": "append"}},
         ]},
        {"name": "PO advancer",
         "description": "call.ended -> business_advance: a completed call from a "
                        "tracked requester or vendor moves their purchase "
                        "(requested -> quoted). Unknown callers and purchases "
                        "already past the move skip honestly.",
         "trigger": {"type": "event_trigger", "params": {"event_type": "call.ended"}},
         "steps": [
             {"type": "business_advance", "name": "Move the tracked purchase",
              "params": {"process": "Purchase lifecycle", "ref": "{{ input.actor }}",
                         "transition": "quote", "actor": "procurement-operator",
                         "note": "a real call ended - the purchase moves itself",
                         "on_missing": "skip", "on_refusal": "skip"}},
         ]},
    ],
    "agent": {"name": "Procurement assistant",
              "greeting": "Buying desk, good day - what are we sourcing?",
              "system_prompt": ("You are a sharp procurement assistant. Answer from the "
                                "knowledge matches in metadata.knowledge; log asks and "
                                "vendor touches; never commit an order - the machine and "
                                "the approvers decide."),
              "knowledge": {"dataset": "Procurement policy", "text_column": "question",
                            "answer_column": "answer", "top_k": 1}},
    "rooms": [{"name": "Vendor room", "title": "Vendor room", "modality": "audio"}],
    "queues": [{"name": "Procurement desk", "room": "Vendor room",
                "bind_agent": True,
                "config": {"max_size": 20, "max_wait_seconds": 300,
                           "announce": {"enabled": True, "interval_seconds": 90},
                           "sms": {"enabled": True, "channel_id": "", "template": ""},
                           "callback": {"enabled": True, "endpoint_id": ""}}}],
    "campaign": None,
    "processes": [
        {"name": "Purchase lifecycle",
         "description": ("The buy as a state machine - one tracked instance per "
                         "purchase remembering where it stands across weeks; "
                         "inbound asks open it, vendor calls move it, the door "
                         "nudges a quote that sits past its SLA."),
         "definition": {
             "states": ["requested", "quoted", "approved", "ordered", "receiving",
                        "received", "closed", "rejected"],
             "initial": "requested",
             "transitions": [
                 {"name": "quote", "from": "requested", "to": "quoted"},
                 {"name": "approve", "from": "quoted", "to": "approved"},
                 {"name": "reject", "from": "quoted", "to": "rejected"},
                 {"name": "order", "from": "approved", "to": "ordered"},
                 {"name": "start_receiving", "from": "ordered", "to": "receiving"},
                 {"name": "confirm", "from": "receiving", "to": "received"},
                 {"name": "close", "from": "received", "to": "closed"},
                 {"name": "escalate", "from": "requested", "to": "requested",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "quoted", "to": "quoted",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "approved", "to": "approved",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "ordered", "to": "ordered",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "receiving", "to": "receiving",
                  "description": "SLA breach nudge - the door's move"},
             ],
         },
         "seed_from_dataset": "Purchase requests",
         "ref_column": "phone",
         "title_from": ["request", "item"],
         "state_column": "status",
         "due_in_seconds": 5 * 24 * 3600,
        },
    ],
    "dashboard": {"name": "Procurement Operator Board",
                  "description": "The buying at a glance - purchases by status, vendor "
                                 "traffic, quote coverage."},
    "notes": [
        "The intake opens BOTH the request row and a tracked instance (ref = the "
        "requester's phone, on_duplicate=skip). It installs INACTIVE - boot the "
        "system to open the reactive path.",
        "The machine defines escalate self-loops on every working state - the "
        "scheduler door sweeps past-SLA purchases through them (POST "
        "/scheduler/escalations/tick).",
        "Cross-operator journeys fire here: landing on 'ordered' opens the "
        "delivery on the Logistics operator's machine (ref = the phone on "
        "file, the leg carries its own SLA) - the goods' journey starts as a "
        "state change.",
        "The policy dataset is the assistant's knowledge - edit it and the "
        "answers follow, no redeploy.",
    ],
}

_LOGISTICS_OPERATOR = {
    "slug": "logistics-operator",
    "name": "Logistics Operator",
    "tagline": ("Delivery ops in one click: orders that intake themselves from "
                "inbound texts and land tracked in the dispatch machine, a "
                "confirmation campaign ready for your endpoint, a dispatch agent "
                "grounded in the delivery FAQ, and the staff board."),
    "category": "Logistics",
    "icon": "truck",
    "color": "#f87171",
    "outcomes": [
        "Orders dataset (deliveries + statuses)",
        "Delivery pipeline business process (pre-wired, seeded from the orders)",
        "Dispatch agent grounded in the delivery FAQ",
        "Dispatch line queue (announce + SMS + callback wired)",
        "Delivery confirmation campaign (empty dialer, targets from the orders)",
        "sms.received -> order intake (event-reactive: row AND tracked order)",
        "call.ended -> delivery logger + delivery advancer (event-reactive)",
        "Delivery events dataset (every touch)",
        "Staff dashboard over the deliveries",
    ],
    "datasets": [
        {"name": "Orders",
         "description": "The board - one row per delivery; the intake workflow "
                        "appends inbound orders, the team works the statuses",
         "columns": ["order", "customer", "phone", "destination", "status"],
         "rows": [
             {"order": "ORD-2041", "customer": "Marta Vidal",
              "phone": "+15550008111", "destination": "12 Harbour Rd",
              "status": "in_transit"},
             {"order": "ORD-2042", "customer": "Chen Wei",
              "phone": "+15550008222", "destination": "4 Mill Lane",
              "status": "dispatched"},
             {"order": "ORD-2043", "customer": "Ike Umeh",
              "phone": "+15550008333", "destination": "88 Ring Rd",
              "status": "placed"},
         ]},
        {"name": "Delivery events",
         "description": "One row per touch (the logger appends every confirmation "
                        "call) - the trail behind every delivery",
         "columns": ["order_ref", "action", "at"],
         "rows": []},
        {"name": "Logistics FAQ",
         "description": "The dispatch agent's knowledge - the answers customers get",
         "columns": ["question", "answer"],
         "rows": [
             {"question": "When will my order arrive",
              "answer": "Delivery windows are two to five working days from dispatch; the machine tracks your order and the confirmation call goes out the moment it is out for delivery."},
             {"question": "I missed the delivery",
              "answer": "The driver reattempts next working day; or reply to arrange a pickup point - your order moves to exception and back by itself."},
             {"question": "Can I change the delivery address",
              "answer": "Before dispatch, yes - tell the agent and the order record updates; after dispatch the reroute goes through the exception path."},
             {"question": "My order arrived damaged",
              "answer": "Say so on the call or text the line - the order moves to exception, the claim opens, and the return is arranged from the same record."},
             {"question": "Do I get proof of delivery",
              "answer": "Yes - the delivered move records the confirmation call on the trail; the note and timestamp stand as the proof."},
         ]},
    ],
    "workflows": [
        {"name": "Order intake",
         "description": "sms.received -> shape -> Orders -> business_start: every "
                        "inbound order opens a row AND a tracked instance (ref = "
                        "the customer's phone) - the dispatch machine watches it "
                        "from day one.",
         "trigger": {"type": "event_trigger", "params": {"event_type": "sms.received"}},
         "steps": [
             {"type": "python_transform", "name": "Shape the order",
              "params": {"code": ("r = df.iloc[0] if len(df) else {}\n"
                                  "pl = r.get('payload') or {}\n"
                                  "result = [{'order': '', "
                                  "'customer': (pl.get('text') or '')[:40], "
                                  "'phone': r.get('actor', ''), 'destination': '', "
                                  "'status': 'placed'}]")}},
             {"type": "dataset_write", "name": "Open the order row",
              "params": {"dataset": "Orders", "mode": "append"}},
             {"type": "business_start", "name": "Track the delivery",
              "params": {"process": "Delivery pipeline",
                         "ref": "{{ nodes.n_trigger.output.actor }}",
                         "title": "Order from {{ nodes.n_trigger.output.actor }}",
                         "actor": "logistics-operator",
                         "due_in_seconds": 172800,
                         "on_duplicate": "skip"}},
         ]},
        {"name": "Delivery logger",
         "description": "call.ended -> shape -> Delivery events. Confirmation calls "
                        "land on the trail - the evidence behind every delivery.",
         "trigger": {"type": "event_trigger", "params": {"event_type": "call.ended"}},
         "steps": [
             {"type": "python_transform", "name": "Shape the touch",
              "params": {"code": ("r = df.iloc[0] if len(df) else {}\n"
                                  "pl = r.get('payload') or {}\n"
                                  "result = [{'order_ref': '', "
                                  "'action': 'call ' + str(pl.get('end_reason', 'ended')), "
                                  "'at': r.get('triggered_at', '')}]")}},
             {"type": "dataset_write", "name": "Append the delivery touch",
              "params": {"dataset": "Delivery events", "mode": "append"}},
         ]},
        {"name": "Delivery advancer",
         "description": "call.ended -> business_advance: a completed confirmation "
                        "call from a tracked customer completes their delivery "
                        "(in_transit -> delivered). Customers the machine does not "
                        "track, and orders already past the move, skip honestly.",
         "trigger": {"type": "event_trigger", "params": {"event_type": "call.ended"}},
         "steps": [
             {"type": "business_advance", "name": "Complete the customer's delivery",
              "params": {"process": "Delivery pipeline", "ref": "{{ input.actor }}",
                         "transition": "deliver", "actor": "logistics-operator",
                         "note": "the confirmation call ended - the delivery completes itself",
                         "on_missing": "skip", "on_refusal": "skip"}},
         ]},
    ],
    "agent": {"name": "Dispatch agent",
              "greeting": "Dispatch here - where is your order headed?",
              "system_prompt": ("You are a brisk, helpful dispatch agent. Answer from the "
                                "knowledge matches in metadata.knowledge; confirm deliveries "
                                "on the call; route damage and address changes to the "
                                "exception path - never promise a time the board has not set."),
              "knowledge": {"dataset": "Logistics FAQ", "text_column": "question",
                            "answer_column": "answer", "top_k": 1}},
    "rooms": [{"name": "Dispatch room", "title": "Dispatch room", "modality": "audio"}],
    "queues": [{"name": "Dispatch line", "room": "Dispatch room",
                "bind_agent": True,
                "config": {"max_size": 40, "max_wait_seconds": 300,
                           "announce": {"enabled": True, "interval_seconds": 60},
                           "sms": {"enabled": True, "channel_id": "", "template": ""},
                           "callback": {"enabled": True, "endpoint_id": ""}}}],
    "campaign": {"name": "Delivery confirmation campaign",
                 "config": {}},
    "processes": [
        {"name": "Delivery pipeline",
         "description": ("The delivery as a state machine - one tracked instance "
                         "per order remembering where it stands across days; "
                         "inbound orders open it, confirmation calls complete it, "
                         "exceptions route through their own path and back, and "
                         "the door nudges a delivery that sits stuck."),
         "definition": {
             "states": ["placed", "picked", "dispatched", "in_transit", "delivered",
                        "exception", "returned"],
             "initial": "placed",
             "transitions": [
                 {"name": "pick", "from": "placed", "to": "picked"},
                 {"name": "dispatch", "from": "picked", "to": "dispatched"},
                 {"name": "depart", "from": "dispatched", "to": "in_transit"},
                 {"name": "deliver", "from": "in_transit", "to": "delivered"},
                 {"name": "raise_exception", "from": "in_transit", "to": "exception"},
                 {"name": "restore", "from": "exception", "to": "in_transit"},
                 {"name": "return_back", "from": "exception", "to": "returned"},
                 {"name": "escalate", "from": "placed", "to": "placed",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "picked", "to": "picked",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "dispatched", "to": "dispatched",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "in_transit", "to": "in_transit",
                  "description": "SLA breach nudge - the door's move"},
             ],
         },
         "seed_from_dataset": "Orders",
         "ref_column": "phone",
         "title_from": ["order", "customer"],
         "state_column": "status",
         "due_in_seconds": 2 * 24 * 3600,
        },
    ],
    "dashboard": {"name": "Logistics Operator Board",
                  "description": "The deliveries at a glance - orders by status, exception "
                                 "rate, confirmation traffic."},
    "notes": [
        "The confirmation campaign installs EMPTY on purpose: add targets from "
        "the orders (POST /voice/campaigns/{id}/targets) and bind a telnyx voice "
        "endpoint when the dialing credentials exist - until then dials skip "
        "honestly.",
        "The intake opens BOTH the order row and a tracked instance (ref = the "
        "customer's phone, on_duplicate=skip). It installs INACTIVE - boot the "
        "system to open the reactive path.",
        "The machine defines escalate self-loops on every working state - the "
        "scheduler door sweeps past-SLA deliveries through them (POST "
        "/scheduler/escalations/tick).",
        "Cross-operator journeys fire here: the Procurement operator's purchase "
        "lifecycle opens the delivery on 'ordered', and the machine fires its "
        "own leg on 'delivered' - the Finance operator's invoice lifecycle "
        "opens the vendor's bill by itself (goods received -> payable, the "
        "three-way match as state changes).",
        "The FAQ dataset is the agent's knowledge - edit it and the answers "
        "follow, no redeploy.",
    ],
}


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


OPERATORS: list[dict] = [
    _MEETING_OPERATOR, _SALES_OPERATOR, _CLINIC_OPERATOR,
    _SUPPORT_OPERATOR, _OPERATIONS_OPERATOR, _HR_OPERATOR,
    _FINANCE_OPERATOR, _PROCUREMENT_OPERATOR, _LOGISTICS_OPERATOR,
]
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
      not count as late);
    * per LEG: ``in_state`` = the source process's open instances sitting
      in the fire state right now (the hand-off is armed), ``fired`` =
      the target process's OPEN instances this leg opened itself (they
      carry the journey link in their context), ``overdue`` = the fired
      ones past the leg's own SLA promise.

    A system that binds no processes draws no chains - the section is
    simply absent, never an empty lie."""
    from datetime import datetime as _dt, timezone as _tz

    from ..models import BusinessProcess, BusinessProcessInstance
    from . import business_processes as process_svc

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

    nodes_stat: dict[str, dict] = {}
    for name, proc in bound.items():
        rows = [r for r in open_rows if r.process_id == proc.id]
        term = terminal_of.get(name) or set()
        nodes_stat[name] = {
            "open": len(rows),
            "overdue": len([r for r in rows
                            if r.due_at is not None and r.state not in term
                            and _late(r)]),
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
            leg_out.append({
                "from_process": src, "on_state": leg["on_state"],
                "opens": dst,
                "due_in_seconds": leg.get("due_in_seconds"),
                "from_operator": owner_of.get(src, ""),
                "opens_operator": owner_of.get(dst, ""),
                "bound_from": bound_from, "bound_opens": bound_opens,
                "counts": {"in_state": in_state, "fired": len(children),
                           "overdue": len([r for r in children if _late(r)])},
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
