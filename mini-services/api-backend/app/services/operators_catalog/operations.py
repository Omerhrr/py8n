"""Operations / Supply Operator - the pre-wired shelf entry (split from app/services/operators.py, task #3).

Moved verbatim - pure declarative data, no behavior change.
"""

OPERATOR = {
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
