"""Curated workflow templates — one-click starting points for new users.

Every template graph is validated with the same ``validate_graph_document``
gate as user workflows, so a template can never ship a broken graph. Keep
templates offline-safe: nodes that call external services use dry-run /
placeholder parameters and note the requirement in their ``docs``.
"""

from __future__ import annotations

from typing import Any

TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "ai-writer",
        "name": "AI Copywriter",
        "description": "Turn a topic into publish-ready marketing copy with a free LLM.",
        "category": "AI",
        "icon": "brain",
        "docs": "Press Run and edit the topic in the Manual Trigger. Uses the built-in LLM bridge — no API key needed.",
        "graph": {
            "nodes": [
                {"id": "topic", "type": "manual_trigger", "name": "Topic", "position": {"x": 0, "y": 0},
                 "parameters": {"payload": {"topic": "Why small teams ship faster", "tone": "confident"}}},
                {"id": "write", "type": "llm_chat", "name": "Draft Copy", "position": {"x": 220, "y": 0},
                 "parameters": {"system_prompt": "You are a senior marketing copywriter. Write tight, vivid copy.",
                                "user_prompt": "Write a 120-word product blog intro about: {{ nodes.topic.output.payload.topic }} (tone: {{ nodes.topic.output.payload.tone }})"}},
                {"id": "shape", "type": "set_variable", "name": "Package", "position": {"x": 440, "y": 0},
                 "parameters": {"assignments": {"topic": "{{ nodes.topic.output.payload.topic }}", "copy": "{{ nodes.write.output.text }}"}, "keep_input": False}},
            ],
            "edges": [
                {"id": "e1", "source": "topic", "target": "write"},
                {"id": "e2", "source": "write", "target": "shape"},
            ],
        },
    },
    {
        "id": "approval-gate",
        "name": "Approval Gate (human-in-the-loop)",
        "description": "Pause for a manager decision and resume with their verdict via API.",
        "category": "Control",
        "icon": "pause-circle",
        "docs": "Run pauses at Wait for Resume. POST {\"approved\": true, \"note\": \"...\"} to the resume URL (shown on the Executions page) to continue.",
        "graph": {
            "nodes": [
                {"id": "req", "type": "manual_trigger", "name": "Expense Request", "position": {"x": 0, "y": 0},
                 "parameters": {"payload": {"ticket": "EXP-42", "amount": 1290}}},
                {"id": "gate", "type": "wait_for_resume", "name": "Manager Approval", "position": {"x": 220, "y": 0},
                 "parameters": {"resume_hint": "Manager: POST {\"approved\": true, \"note\": \"...\"} to resume"}},
                {"id": "verdict", "type": "set_variable", "name": "Verdict", "position": {"x": 440, "y": 0},
                 "parameters": {"assignments": {"ticket": "{{ nodes.req.output.payload.ticket }}",
                                                "approved": "{{ nodes.gate.output.approved }}",
                                                "note": "{{ nodes.gate.output.note }}"}, "keep_input": False}},
            ],
            "edges": [
                {"id": "e1", "source": "req", "target": "gate"},
                {"id": "e2", "source": "gate", "target": "verdict"},
            ],
        },
    },
    {
        "id": "order-batch-digest",
        "name": "Order Batch Digest",
        "description": "Slice orders into batches, compute per-batch revenue, aggregate a summary.",
        "category": "Data",
        "icon": "repeat",
        "docs": "Demonstrates Loop Over Items: the body runs per batch, done output feeds the aggregate.",
        "graph": {
            "nodes": [
                {"id": "in", "type": "manual_trigger", "name": "Orders", "position": {"x": 0, "y": 0},
                 "parameters": {"payload": {"items": [
                     {"id": "A1", "revenue": 19.75}, {"id": "A2", "revenue": 120.5},
                     {"id": "B1", "revenue": 20.0}, {"id": "B2", "revenue": 68.75},
                     {"id": "C1", "revenue": 28.9},
                 ]}}},
                {"id": "loop", "type": "loop_over_items", "name": "Batch 2", "position": {"x": 220, "y": 0},
                 "parameters": {"items_path": "items", "batch_size": 2}},
                {"id": "rev", "type": "code", "name": "Batch Revenue", "position": {"x": 440, "y": -40},
                 "parameters": {"code": "items = input_data['items']\nresult = {'revenue': round(sum(i['revenue'] for i in items), 2), 'orders': len(items)}\n"}},
                {"id": "sum", "type": "aggregate", "name": "Totals", "position": {"x": 660, "y": 40},
                 "parameters": {"mode": "sum", "field": "result.revenue"}},
            ],
            "edges": [
                {"id": "e1", "source": "in", "target": "loop"},
                {"id": "e2", "source": "loop", "target": "rev", "sourceHandle": "loop"},
                {"id": "e3", "source": "loop", "target": "sum", "sourceHandle": "done"},
            ],
        },
    },
    {
        "id": "lead-router",
        "name": "Lead Router (IF)",
        "description": "Webhook intake that routes hot leads down a different branch.",
        "category": "Integrations",
        "icon": "git-branch",
        "docs": "POST to the workflow's webhook URL: {\"lead\": {\"score\": 85, \"email\": \"...\"}}. Hot leads (score >= 70) get the priority branch.",
        "graph": {
            "nodes": [
                {"id": "hook", "type": "webhook_trigger", "name": "Lead Intake", "position": {"x": 0, "y": 0},
                 "parameters": {"response_mode": "last_node"}},
                {"id": "route", "type": "if_condition", "name": "Hot Lead?", "position": {"x": 220, "y": 0},
                 "parameters": {"left_value": "{{ nodes.hook.output.body.lead.score }}", "operator": "greater_than", "right_value": 70}},
                {"id": "hot", "type": "set_variable", "name": "Priority Queue", "position": {"x": 440, "y": -60},
                 "parameters": {"assignments": {"queue": "priority", "email": "{{ nodes.hook.output.body.lead.email }}"}, "keep_input": False}},
                {"id": "nurture", "type": "set_variable", "name": "Nurture Queue", "position": {"x": 440, "y": 80},
                 "parameters": {"assignments": {"queue": "nurture", "email": "{{ nodes.hook.output.body.lead.email }}"}, "keep_input": False}},
            ],
            "edges": [
                {"id": "e1", "source": "hook", "target": "route"},
                {"id": "e2", "source": "route", "target": "hot", "sourceHandle": "true"},
                {"id": "e3", "source": "route", "target": "nurture", "sourceHandle": "false"},
            ],
        },
    },
    {
        "id": "api-poller",
        "name": "Scheduled API Poller",
        "description": "Hit an HTTP endpoint every 15 minutes and flag slow responses.",
        "category": "Ops",
        "icon": "globe",
        "docs": "Enable the workflow (Triggers toggle) to arm the schedule. Uses Py8n's own health endpoint as a placeholder — swap the URL to anything.",
        "graph": {
            "nodes": [
                {"id": "tick", "type": "schedule_trigger", "name": "Every 15 min", "position": {"x": 0, "y": 0},
                 "parameters": {"interval_seconds": 900}},
                {"id": "ping", "type": "http_request", "name": "GET Status", "position": {"x": 220, "y": 0},
                 "parameters": {"method": "GET", "url": "http://127.0.0.1:8000/api/v1/node-definitions", "timeout_seconds": 10}},
                {"id": "check", "type": "code", "name": "Latency Check", "position": {"x": 440, "y": 0},
                 "parameters": {"code": "result = {'ok': input_data['status'] == 200, 'checked_at': input_data.get('headers', {}).get('date', 'n/a')}\n"}},
            ],
            "edges": [
                {"id": "e1", "source": "tick", "target": "ping"},
                {"id": "e2", "source": "ping", "target": "check"},
            ],
        },
    },
    {
        "id": "webhook-slack-alert",
        "name": "Webhook → Slack Alert",
        "description": "Format an incoming webhook and push a Slack notification.",
        "category": "Integrations",
        "icon": "slack",
        "docs": "Add a Slack incoming-webhook URL (or a bot-token credential) on the Alert node and set dry_run=false. dry_run=true previews the payload safely.",
        "graph": {
            "nodes": [
                {"id": "hook", "type": "webhook_trigger", "name": "Incident", "position": {"x": 0, "y": 0},
                 "parameters": {"response_mode": "immediately"}},
                {"id": "fmt", "type": "set_variable", "name": "Format", "position": {"x": 220, "y": 0},
                 "parameters": {"assignments": {"text": "Incident: {{ nodes.hook.output.body.title | default('Untitled') }} — severity {{ nodes.hook.output.body.severity | default('n/a') }}"}, "keep_input": False}},
                {"id": "alert", "type": "slack_message", "name": "Slack Alert", "position": {"x": 440, "y": 0},
                 "parameters": {"text": "{{ nodes.fmt.output.text }}", "dry_run": True}},
            ],
            "edges": [
                {"id": "e1", "source": "hook", "target": "fmt"},
                {"id": "e2", "source": "fmt", "target": "alert"},
            ],
        },
    },
    {
        "id": "data-pipeline",
        "name": "Split → Filter → Aggregate",
        "description": "Canonical data pipeline: explode a list, keep what matters, roll it up.",
        "category": "Data",
        "icon": "sigma",
        "docs": "Pure data-shaping demo — runs instantly, no external calls.",
        "graph": {
            "nodes": [
                {"id": "in", "type": "manual_trigger", "name": "Orders", "position": {"x": 0, "y": 0},
                 "parameters": {"payload": {"items": [
                     {"region": "eu", "amount": 120}, {"region": "us", "amount": 80},
                     {"region": "eu", "amount": 220}, {"region": "apac", "amount": 30},
                 ]}}},
                {"id": "split", "type": "split_out", "name": "Explode", "position": {"x": 220, "y": 0},
                 "parameters": {"field": "items"}},
                {"id": "keep", "type": "filter", "name": "EU ≥ 100", "position": {"x": 440, "y": 0},
                 "parameters": {"field": "amount", "operator": "greater_than", "right_value": 100}},
                {"id": "total", "type": "aggregate", "name": "EU Total", "position": {"x": 660, "y": 0},
                 "parameters": {"mode": "sum", "field": "amount"}},
            ],
            "edges": [
                {"id": "e1", "source": "in", "target": "split"},
                {"id": "e2", "source": "split", "target": "keep"},
                {"id": "e3", "source": "keep", "target": "total"},
            ],
        },
    },
    {
        "id": "daily-digest",
        "name": "Daily Digest Email",
        "description": "Every morning: build a digest of yesterday's numbers and email it.",
        "category": "Ops",
        "icon": "mail",
        "docs": "dry_run=true previews the email. Attach an SMTP credential and set dry_run=false to actually send.",
        "graph": {
            "nodes": [
                {"id": "morning", "type": "schedule_trigger", "name": "08:00 Daily", "position": {"x": 0, "y": 0},
                 "parameters": {"interval_seconds": 86400}},
                {"id": "stats", "type": "code", "name": "Collect Stats", "position": {"x": 220, "y": 0},
                 "parameters": {"code": "import datetime\ntoday = datetime.date.today().isoformat()\nresult = {'day': today, 'runs': 42, 'failures': 1, 'top_workflow': 'Lead Router'}\n"}},
                {"id": "mail", "type": "email_send", "name": "Send Digest", "position": {"x": 440, "y": 0},
                 "parameters": {"to": "ops@example.com", "subject": "Py8n digest {{ nodes.stats.output.day }}",
                                "body": "Runs: {{ nodes.stats.output.runs }} · Failures: {{ nodes.stats.output.failures }} · Top: {{ nodes.stats.output.top_workflow }}",
                                "dry_run": True}},
            ],
            "edges": [
                {"id": "e1", "source": "morning", "target": "stats"},
                {"id": "e2", "source": "stats", "target": "mail"},
            ],
        },
    },
]


def get_template(template_id: str) -> dict[str, Any] | None:
    return next((t for t in TEMPLATES if t["id"] == template_id), None)


def template_summary(t: dict[str, Any]) -> dict[str, Any]:
    nodes = t["graph"].get("nodes", [])
    return {
        "id": t["id"],
        "name": t["name"],
        "description": t["description"],
        "category": t["category"],
        "icon": t["icon"],
        "docs": t["docs"],
        "node_count": len(nodes),
        "node_types": [n["type"] for n in nodes],
    }
