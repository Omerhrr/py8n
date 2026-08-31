"""Curated workflow templates - one-click starting points for new users.

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
        "docs": "Press Run and edit the topic in the Manual Trigger. Uses the built-in LLM bridge - no API key needed.",
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
        "docs": "Enable the workflow (Triggers toggle) to arm the schedule. Uses Py8n's own health endpoint as a placeholder - swap the URL to anything.",
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
                 "parameters": {"assignments": {"text": "Incident: {{ nodes.hook.output.body.title | default('Untitled') }} - severity {{ nodes.hook.output.body.severity | default('n/a') }}"}, "keep_input": False}},
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
        "docs": "Pure data-shaping demo - runs instantly, no external calls.",
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
    # ------------------------------------------------------------------
    # v33 - readymade automations showcasing the v19-v32 stack
    # ------------------------------------------------------------------
    {
        "id": "invoice-to-books",
        "name": "Invoice → Books (Document AI)",
        "description": "Extract the line-items table from an invoice file and append it to a ledger dataset.",
        "category": "Document AI",
        "icon": "file-search",
        "badge": "Doc AI",
        "tags": ["invoice", "ocr", "pdf", "document", "extract", "dataset", "accounting"],
        "docs": "Run with a real file: edit the Manual Trigger's invoice_path (server-side path to a PDF/image/Word/Excel/CSV). The Extract node reads it (OCR for scans), emits the best table as items, and Dataset Write appends every row to 'Invoice Ledger' - instantly SQL-queryable and dashboard-ready.",
        "graph": {
            "nodes": [
                {"id": "in", "type": "manual_trigger", "name": "Invoice Path", "position": {"x": 0, "y": 0},
                 "parameters": {"payload": {"invoice_path": "/path/to/invoice.pdf"}}},
                {"id": "doc", "type": "document_extract", "name": "Extract Table", "position": {"x": 220, "y": 0},
                 "parameters": {"source": "path", "path": "{{ nodes.in.output.payload.invoice_path }}",
                                "include_items": True, "coerce_numbers": True}},
                {"id": "books", "type": "dataset_write", "name": "Append to Ledger", "position": {"x": 440, "y": 0},
                 "parameters": {"dataset": "Invoice Ledger", "mode": "append", "create_if_missing": True}},
            ],
            "edges": [
                {"id": "e1", "source": "in", "target": "doc"},
                {"id": "e2", "source": "doc", "target": "books"},
            ],
        },
    },
    {
        "id": "uptime-sentinel",
        "name": "Uptime Sentinel → Dataset",
        "description": "Ping an endpoint every 5 minutes and build a queryable uptime history dataset.",
        "category": "Ops",
        "icon": "activity",
        "badge": "Dataset",
        "tags": ["uptime", "monitoring", "schedule", "health", "history", "sql"],
        "docs": "Enable the workflow (Triggers toggle) to arm the schedule - every 5 minutes it pings the URL, records status + latency, and appends to the 'Uptime Log' dataset. Point Dataset Write at any dataset to chart it in a Dashboard or build an App on top. The default URL is Py8n's own health endpoint.",
        "graph": {
            "nodes": [
                {"id": "tick", "type": "schedule_trigger", "name": "Every 5 min", "position": {"x": 0, "y": 0},
                 "parameters": {"mode": "interval", "interval_seconds": 300}},
                {"id": "ping", "type": "http_request", "name": "Ping Endpoint", "position": {"x": 220, "y": 0},
                 "parameters": {"method": "GET", "url": "http://127.0.0.1:8000/api/v1/health", "timeout_seconds": 10}},
                {"id": "rec", "type": "set_variable", "name": "Record Sample", "position": {"x": 440, "y": 0},
                 "parameters": {"assignments": {
                     "checked_at": "{{ now }}",
                     "ok": "{{ nodes.ping.output.status == 200 }}",
                     "status": "{{ nodes.ping.output.status }}"},
                     "keep_input": False}},
                {"id": "log", "type": "dataset_write", "name": "Append Uptime Log", "position": {"x": 660, "y": 0},
                 "parameters": {"dataset": "Uptime Log", "mode": "append", "create_if_missing": True}},
            ],
            "edges": [
                {"id": "e1", "source": "tick", "target": "ping"},
                {"id": "e2", "source": "ping", "target": "rec"},
                {"id": "e3", "source": "rec", "target": "log"},
            ],
        },
    },
    {
        "id": "research-agent",
        "name": "Research Agent with Tools",
        "description": "An AI agent that consults company knowledge and live HTTP APIs before answering.",
        "category": "AI",
        "icon": "bot",
        "badge": "Agent",
        "tags": ["agent", "tools", "research", "llm", "knowledge", "http"],
        "docs": "Press Run and edit the question in the Manual Trigger. The agent loops: it can call the knowledge tool (company facts - edit them on the node) and hit api.github.com over HTTP, then answers. Uses the built-in LLM bridge - no API key needed. Swap the HTTP tool's allowed domains to whatever you trust.",
        "graph": {
            "nodes": [
                {"id": "q", "type": "manual_trigger", "name": "Question", "position": {"x": 0, "y": 0},
                 "parameters": {"payload": {"question": "How many open issues does the py8n repo have, and what is our refund window?"}}},
                {"id": "agent", "type": "ai_agent", "name": "Researcher", "position": {"x": 220, "y": 0},
                 "parameters": {
                     "provider": "sandbox_bridge",
                     "system_prompt": "You are a meticulous research assistant. Use your tools to check facts before answering; cite which tool you used.",
                     "user_message": "Research and answer: {{ nodes.q.output.payload.question }}",
                     "max_iterations": 5,
                     "temperature": 0.3,
                     "tools": [
                         {"kind": "knowledge", "name": "company_facts",
                          "description": "Internal company policy facts",
                          "content": "Acme Corp facts: refund window is 30 days from purchase. Support SLA: first response within 4 business hours. HQ in Lisbon; EU VAT number PT508889031."},
                         {"kind": "http", "name": "github_api",
                          "description": "Query the public GitHub REST API for repo data",
                          "allowed_domains": ["api.github.com"]},
                     ]}},
            ],
            "edges": [
                {"id": "e1", "source": "q", "target": "agent"},
            ],
        },
    },
    {
        "id": "custom-webhook-reply",
        "name": "Expense API: Auto-Approve / Review",
        "description": "A webhook API that answers 200 for small expenses and 202 review-required for big ones.",
        "category": "Integrations",
        "icon": "reply",
        "badge": "v21",
        "tags": ["webhook", "respond", "api", "approval", "expense", "routing"],
        "docs": "POST {\"expense\": {\"id\": \"E-1\", \"amount\": 45}} to the workflow's webhook URL. response_mode=respond_node means the Respond nodes craft the actual HTTP reply: amounts over 1000 answer 202 {needs_review} inline, everything else 200 {approved}. No polling needed - the caller gets the verdict in the same request.",
        "graph": {
            "nodes": [
                {"id": "hook", "type": "webhook_trigger", "name": "Expense Intake", "position": {"x": 0, "y": 0},
                 "parameters": {"response_mode": "respond_node"}},
                {"id": "route", "type": "if_condition", "name": "Over 1000?", "position": {"x": 220, "y": 0},
                 "parameters": {"left_value": "{{ nodes.hook.output.body.expense.amount }}", "operator": "greater_than", "right_value": 1000}},
                {"id": "review", "type": "respond_to_webhook", "name": "202 Needs Review", "position": {"x": 440, "y": -70},
                 "parameters": {"status_code": 202,
                                "body": "{\"expense_id\": \"{{ nodes.hook.output.body.expense.id }}\", \"status\": \"needs_review\", \"reason\": \"amount over 1000\"}",
                                "content_type": "application/json"}},
                {"id": "ok", "type": "respond_to_webhook", "name": "200 Approved", "position": {"x": 440, "y": 70},
                 "parameters": {"status_code": 200,
                                "body": "{\"expense_id\": \"{{ nodes.hook.output.body.expense.id }}\", \"status\": \"approved\"}",
                                "content_type": "application/json"}},
            ],
            "edges": [
                {"id": "e1", "source": "hook", "target": "route"},
                {"id": "e2", "source": "route", "target": "review", "sourceHandle": "true"},
                {"id": "e3", "source": "route", "target": "ok", "sourceHandle": "false"},
            ],
        },
    },
    {
        "id": "error-responder",
        "name": "Error Handler: Alert & Format",
        "description": "A dedicated error-handler workflow that formats failures and raises a Slack alert.",
        "category": "Control",
        "icon": "siren",
        "badge": "Resilience",
        "tags": ["error", "alert", "resilience", "failure", "slack", "handler"],
        "docs": "Set this workflow as the Error workflow of any other workflow (workflow settings → Error workflow). When that workflow fails, this one starts with the structured error payload {workflow_name, error, failed_nodes} - it formats a summary and previews a Slack alert (dry_run=true; attach a credential and set dry_run=false to post for real).",
        "graph": {
            "nodes": [
                {"id": "err", "type": "error_trigger", "name": "On Failure", "position": {"x": 0, "y": 0},
                 "parameters": {"include_failed_nodes": True}},
                {"id": "fmt", "type": "set_variable", "name": "Format Summary", "position": {"x": 220, "y": 0},
                 "parameters": {"assignments": {
                     "title": "Pipeline failed: {{ nodes.err.output.workflow_name }}",
                     "error": "{{ nodes.err.output.error | truncate(300) }}"},
                     "keep_input": False}},
                {"id": "alert", "type": "slack_message", "name": "Slack Alert", "position": {"x": 440, "y": 0},
                 "parameters": {"text": "{{ nodes.fmt.output.title }} - {{ nodes.fmt.output.error }}", "dry_run": True}},
            ],
            "edges": [
                {"id": "e1", "source": "err", "target": "fmt"},
                {"id": "e2", "source": "fmt", "target": "alert"},
            ],
        },
    },
    {
        "id": "support-chatbot",
        "name": "Support Chatbot (memory)",
        "description": "A chat-panel assistant that remembers the conversation and answers from an FAQ.",
        "category": "AI",
        "icon": "message-square",
        "badge": "Chat",
        "tags": ["chat", "support", "agent", "memory", "faq", "assistant"],
        "docs": "Open the workflow editor and use the Chat panel: each message starts a run and the agent's answer streams back. memory=buffer + session_id means the agent remembers prior turns per session. Edit the FAQ knowledge tool on the node to ground answers in your own content. Runs on the built-in LLM bridge.",
        "graph": {
            "nodes": [
                {"id": "chat", "type": "chat_trigger", "name": "Chat", "position": {"x": 0, "y": 0},
                 "parameters": {"response_mode": "last_node",
                                "welcome_message": "Hi! Ask me about Acme Corp policies, orders or refunds."}},
                {"id": "agent", "type": "ai_agent", "name": "Support Agent", "position": {"x": 220, "y": 0},
                 "parameters": {
                     "provider": "sandbox_bridge",
                     "system_prompt": "You are Acme Corp's support agent. Answer briefly and helpfully; ground policy answers in the FAQ tool; if you don't know, say so and offer to escalate.",
                     "user_message": "{{ nodes.chat.output.message }}",
                     "memory": "buffer",
                     "session_key": "support-{{ nodes.chat.output.session_id }}",
                     "max_history_turns": 6,
                     "tools": [
                         {"kind": "knowledge", "name": "faq",
                          "description": "Acme Corp support FAQ",
                          "content": "Refunds: within 30 days of purchase, no questions asked. Shipping: EU 2-4 days, worldwide 7-14 days. Plans: Starter (free), Team (29/mo), Business (99/mo). Password reset: Settings → Security → Reset."},
                     ]}},
            ],
            "edges": [
                {"id": "e1", "source": "chat", "target": "agent"},
            ],
        },
    },
    {
        "id": "csv-join-report",
        "name": "Two-Dataset SQL Join Report",
        "description": "Load demo orders + customers into datasets, then JOIN them with DuckDB SQL.",
        "category": "Data",
        "icon": "table-2",
        "badge": "SQL",
        "tags": ["sql", "join", "duckdb", "datasets", "report", "revenue"],
        "docs": "Fully offline demo of the dataset engine: one run writes two small datasets (replace mode, so it's re-runnable), then a SQL node joins them per customer. Run it once, then open Datasets → demo_orders / demo_customers, or paste the SQL into the SQL Console. Cross-dataset joins are the v27 flagship.",
        "graph": {
            "nodes": [
                {"id": "in", "type": "manual_trigger", "name": "Start", "position": {"x": 0, "y": 0},
                 "parameters": {"payload": {
                     "orders": [
                         {"order_id": "O1", "customer_id": "C1", "amount": 120.0},
                         {"order_id": "O2", "customer_id": "C2", "amount": 80.5},
                         {"order_id": "O3", "customer_id": "C1", "amount": 220.0},
                         {"order_id": "O4", "customer_id": "C3", "amount": 45.25},
                     ],
                     "customers": [
                         {"id": "C1", "name": "Acme Corp", "region": "eu"},
                         {"id": "C2", "name": "Globex", "region": "us"},
                         {"id": "C3", "name": "Initech", "region": "us"},
                     ]}}},
                {"id": "sv1", "type": "set_variable", "name": "Orders Rows", "position": {"x": 220, "y": -80},
                 "parameters": {"assignments": {"items": "{{ nodes.in.output.payload.orders }}"}, "keep_input": False}},
                {"id": "sv2", "type": "set_variable", "name": "Customer Rows", "position": {"x": 220, "y": 90},
                 "parameters": {"assignments": {"items": "{{ nodes.in.output.payload.customers }}"}, "keep_input": False}},
                {"id": "dw1", "type": "dataset_write", "name": "Write demo_orders", "position": {"x": 440, "y": -80},
                 "parameters": {"dataset": "demo_orders", "mode": "replace", "create_if_missing": True}},
                {"id": "dw2", "type": "dataset_write", "name": "Write demo_customers", "position": {"x": 440, "y": 90},
                 "parameters": {"dataset": "demo_customers", "mode": "replace", "create_if_missing": True}},
                {"id": "sql", "type": "sql_query", "name": "Revenue per Customer", "position": {"x": 660, "y": -80},
                 "parameters": {"sql": "SELECT c.name, c.region, count(*) AS orders, ROUND(SUM(o.amount), 2) AS total\nFROM demo_orders o JOIN demo_customers c ON o.customer_id = c.id\nGROUP BY c.name, c.region ORDER BY total DESC"}},
            ],
            "edges": [
                {"id": "e1", "source": "in", "target": "sv1"},
                {"id": "e2", "source": "sv1", "target": "dw1"},
                {"id": "e3", "source": "dw1", "target": "sql"},
                {"id": "e4", "source": "in", "target": "sv2"},
                {"id": "e5", "source": "sv2", "target": "dw2"},
            ],
        },
    },
    {
        "id": "lead-capture-api",
        "name": "Lead Capture API → Dataset",
        "description": "A public webhook endpoint that validates leads into a dataset and answers 201 JSON.",
        "category": "Integrations",
        "icon": "inbox",
        "badge": "Full-stack",
        "tags": ["webhook", "leads", "crm", "dataset", "api", "capture", "form"],
        "docs": "POST {\"name\": \"Ada\", \"email\": \"ada@x.io\", \"company\": \"X Ltd\"} to the workflow's webhook URL - the lead lands in the 'Leads' dataset and the caller immediately gets 201 {ok, lead}. Build an App or Dashboard on the Leads dataset to work the pipeline. Data-to-application in one template.",
        "graph": {
            "nodes": [
                {"id": "hook", "type": "webhook_trigger", "name": "Lead Intake", "position": {"x": 0, "y": 0},
                 "parameters": {"response_mode": "respond_node"}},
                {"id": "save", "type": "set_variable", "name": "Normalize Lead", "position": {"x": 220, "y": 0},
                 "parameters": {"assignments": {
                     "name": "{{ nodes.hook.output.body.name | default('anonymous') }}",
                     "email": "{{ nodes.hook.output.body.email | default('') }}",
                     "company": "{{ nodes.hook.output.body.company | default('') }}"},
                     "keep_input": False}},
                {"id": "store", "type": "dataset_write", "name": "Append to Leads", "position": {"x": 440, "y": 0},
                 "parameters": {"dataset": "Leads", "mode": "append", "create_if_missing": True}},
                {"id": "reply", "type": "respond_to_webhook", "name": "201 Created", "position": {"x": 660, "y": 0},
                 "parameters": {"status_code": 201,
                                "body": "{\"ok\": true, \"lead\": {\"name\": \"{{ nodes.save.output.name }}\", \"email\": \"{{ nodes.save.output.email }}\"}}",
                                "content_type": "application/json"}},
            ],
            "edges": [
                {"id": "e1", "source": "hook", "target": "save"},
                {"id": "e2", "source": "save", "target": "store"},
                {"id": "e3", "source": "store", "target": "reply"},
            ],
        },
    },
    {
        # v34 - showcases the dataset + code tool kinds on the AI Agent
        "id": "data-analyst",
        "name": "SQL Data Analyst Agent",
        "description": "An AI agent that interrogates your datasets with read-only SQL, computes in sandboxed Python, and answers in plain language.",
        "category": "AI",
        "icon": "bot",
        "badge": "Agent",
        "tags": ["agent", "tools", "dataset", "sql", "duckdb", "code", "analyst"],
        "docs": "Create (or upload) a dataset first - e.g. 'Sales' - then press Run and edit the question in the Manual Trigger. The agent writes its own SELECT against your datasets (DuckDB syntax, tables are dataset names), can double-check arithmetic in a sandboxed Python tool, and answers with the numbers. Strictly read-only: one SELECT statement per call.",
        "graph": {
            "nodes": [
                {"id": "q", "type": "manual_trigger", "name": "Question", "position": {"x": 0, "y": 0},
                 "parameters": {"payload": {"question": "Which rows have the highest value in the Sales dataset, and what is the total across all rows?"}}},
                {"id": "agent", "type": "ai_agent", "name": "Analyst", "position": {"x": 220, "y": 0},
                 "parameters": {
                     "provider": "sandbox_bridge",
                     "system_prompt": "You are a rigorous data analyst. Inspect the data with the SQL tool before answering; verify any arithmetic with the Python tool; state which tables you used.",
                     "user_message": "Datasets are available as SQL views named after them. Question: {{ nodes.q.output.payload.message | default(nodes.q.output.payload.question) }}",
                     "max_iterations": 6,
                     "temperature": 0.2,
                     "tools": [
                         {"kind": "dataset", "name": "sql_query",
                          "description": "Run ONE read-only SELECT (DuckDB syntax) over all datasets - every dataset is a view named after it, e.g. Sales. Arguments: {\"sql\": \"SELECT ...\"}",
                          "max_rows": 25},
                         {"kind": "code", "name": "python_compute",
                          "description": "Sandboxed Python for exact arithmetic/formatting. Set `result`; arguments: {\"code\": \"result = 2 + 2\"}"},
                     ]}},
            ],
            "edges": [
                {"id": "e1", "source": "q", "target": "agent"},
            ],
        },
    },
]


def get_template(template_id: str) -> dict[str, Any] | None:
    return next((t for t in TEMPLATES if t["id"] == template_id), None)


# v33 - per-category accent colors for the gallery cards (single source of truth)
CATEGORY_ACCENT: dict[str, str] = {
    "AI": "#a78bfa",
    "Data": "#38bdf8",
    "Integrations": "#34d399",
    "Control": "#fbbf24",
    "Ops": "#fb7185",
    "Document AI": "#fb923c",
}


def template_summary(t: dict[str, Any]) -> dict[str, Any]:
    nodes = t["graph"].get("nodes", [])
    return {
        "id": t["id"],
        "name": t["name"],
        "description": t["description"],
        "category": t["category"],
        "icon": t["icon"],
        "docs": t["docs"],
        "badge": t.get("badge"),
        "tags": t.get("tags", []),
        "accent": CATEGORY_ACCENT.get(t["category"], "#f97316"),
        "node_count": len(nodes),
        "node_types": [n["type"] for n in nodes],
    }
