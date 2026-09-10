"""Support Operator - the pre-wired shelf entry (split from app/services/operators.py, task #3).

Moved verbatim - pure declarative data, no behavior change.
"""

OPERATOR = {
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
