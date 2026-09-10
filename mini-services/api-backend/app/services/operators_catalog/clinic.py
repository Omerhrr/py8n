"""Clinic / Care Operator - the pre-wired shelf entry (split from app/services/operators.py, task #3).

Moved verbatim - pure declarative data, no behavior change.
"""

OPERATOR = {
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
