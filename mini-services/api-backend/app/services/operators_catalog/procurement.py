"""Procurement Operator - the pre-wired shelf entry (split from app/services/operators.py, task #3).

Moved verbatim - pure declarative data, no behavior change.
"""

OPERATOR = {
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
