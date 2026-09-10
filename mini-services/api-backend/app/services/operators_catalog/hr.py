"""HR Operator - the pre-wired shelf entry (split from app/services/operators.py, task #3).

Moved verbatim - pure declarative data, no behavior change.
"""

OPERATOR = {
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
