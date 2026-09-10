"""Logistics Operator - the pre-wired shelf entry (split from app/services/operators.py, task #3).

Moved verbatim - pure declarative data, no behavior change.
"""

OPERATOR = {
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
