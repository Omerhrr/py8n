"""Sales / Revenue Operator - the pre-wired shelf entry (split from app/services/operators.py, task #3).

Moved verbatim - pure declarative data, no behavior change.
"""

OPERATOR = {
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
