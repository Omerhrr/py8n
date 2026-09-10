"""Meeting Operator - the pre-wired shelf entry (split from app/services/operators.py, task #3).

Moved verbatim - pure declarative data, no behavior change.
"""

OPERATOR = {
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
