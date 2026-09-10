"""Finance Operator - the pre-wired shelf entry (split from app/services/operators.py, task #3).

Moved verbatim - pure declarative data, no behavior change.
"""

OPERATOR = {
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
