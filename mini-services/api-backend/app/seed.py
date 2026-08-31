"""First-run seed: demo workflows that work offline (no external APIs needed)."""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Workflow

logger = logging.getLogger("py8n.seed")


def _node(id_: str, type_: str, name: str, x: float, y: float, params: dict | None = None) -> dict:
    return {"id": id_, "type": type_, "name": name, "position": {"x": x, "y": y}, "parameters": params or {}}


def _edge(id_: str, source: str, target: str, source_handle: str = "main") -> dict:
    return {"id": id_, "source": source, "target": target, "sourceHandle": source_handle, "targetHandle": "main"}


def _quickstart() -> Workflow:
    graph = {
        "nodes": [
            _node("start", "manual_trigger", "Manual Trigger", 0, 0, {"payload": {"name": "Ada", "score": 42}}),
            _node(
                "enrich", "code", "Enrich data", 260, 0,
                {"code": "name = input_data['payload']['name']\nresult = {\n  'greeting': 'Hello ' + name + '!',\n  'score': input_data['payload']['score'],\n  'letter_count': len(name),\n}\n"},
            ),
            _node("check", "if_condition", "Score > 40?", 520, 0,
                  {"left_value": "{{ nodes.enrich.output.result.score }}", "operator": "greater_than", "right_value": 40}),
            _node("win", "set_variable", "Winning message", 780, -90,
                  {"keep_input": False, "assignments": {"message": "{{ nodes.enrich.output.result.greeting }} 🎉", "branch": "high-score"}}),
            _node("tryagain", "set_variable", "Encourage", 780, 90,
                  {"keep_input": False, "assignments": {"message": "Keep practicing, {{ nodes.start.output.payload.name }}!", "branch": "low-score"}}),
        ],
        "edges": [
            _edge("e1", "start", "enrich"),
            _edge("e2", "enrich", "check"),
            _edge("e3", "check", "win", source_handle="true"),
            _edge("e4", "check", "tryagain", source_handle="false"),
        ],
    }
    return Workflow(
        name="Hello Py8n - Quickstart",
        description="Manual trigger → Python code → IF branch demo. Press Run and watch the true/false branches light up.",
        graph=graph,
        is_active=True,
    )


def _ai_writer() -> Workflow:
    graph = {
        "nodes": [
            _node("start", "manual_trigger", "Manual Trigger", 0, 0,
                  {"payload": {"topic": "why automation matters"}}),
            _node("llm", "llm_chat", "AI Brain (free)", 260, 0,
                  {"provider": "sandbox_bridge", "system_prompt": "You are a punchy tech copywriter.",
                   "user_prompt": "Write a 2-sentence micro-blog about {{ nodes.start.output.payload.topic }}.",
                   "temperature": 0.8}),
            _node("format", "set_variable", "Format output", 520, 0,
                  {"keep_input": False, "assignments": {
                      "title": "On {{ nodes.start.output.payload.topic }}",
                      "body": "{{ nodes.llm.output.text }}",
                  }}),
        ],
        "edges": [_edge("e1", "start", "llm"), _edge("e2", "llm", "format")],
    }
    return Workflow(
        name="AI Writer - free LLM demo",
        description="Manual trigger → LLM Chat (built-in free bridge) → Set. No API keys needed.",
        graph=graph,
        is_active=True,
    )


def _webhook_echo() -> Workflow:
    graph = {
        "nodes": [
            _node("hook", "webhook_trigger", "Webhook Trigger", 0, 0, {"response_mode": "last_node"}),
            _node("echo", "set_variable", "Build echo", 260, 0,
                  {"keep_input": False, "assignments": {
                      "you_sent": "{{ nodes.hook.output.body }}",
                      "method": "{{ nodes.hook.output.method }}",
                      "received_at": "{{ now }}",
                  }}),
            _node("shape", "code", "Shape response", 520, 0,
                  {"code": "result = {'echo': input_data['you_sent'], 'ok': True, 'via': input_data['method']}\n"}),
        ],
        "edges": [_edge("e1", "hook", "echo"), _edge("e2", "echo", "shape")],
    }
    return Workflow(
        name="Webhook Echo Bot",
        description="POST anything to the webhook URL and get a structured JSON echo back (response_mode=last_node).",
        graph=graph,
        is_active=True,
    )


def _api_ping() -> Workflow:
    graph = {
        "nodes": [
            _node("start", "manual_trigger", "Manual Trigger", 0, 0, {}),
            _node("http", "http_request", "Call Py8n API", 260, 0,
                  {"method": "GET", "url": "http://127.0.0.1:8000/api/v1/health", "body_type": "none"}),
            _node("report", "set_variable", "Report", 520, 0,
                  {"keep_input": False, "assignments": {
                      "engine_status": "{{ nodes.http.output.body.status }}",
                      "version": "{{ nodes.http.output.body.version }}",
                      "http_code": "{{ nodes.http.output.status_code }}",
                  }}),
        ],
        "edges": [_edge("e1", "start", "http"), _edge("e2", "http", "report")],
    }
    return Workflow(
        name="HTTP Request - self ping",
        description="Manual trigger → HTTP Request node hitting the Py8n health endpoint → Set. Demonstrates the HTTP action node.",
        graph=graph,
        is_active=True,
    )


def _batch_digest() -> Workflow:
    graph = {
        "nodes": [
            _node("start", "manual_trigger", "Manual Trigger", 0, 0,
                  {"payload": {"items": [
                      {"order": "A-101", "amount": 12.5},
                      {"order": "A-102", "amount": 7.25},
                      {"order": "A-103", "amount": 99.0},
                      {"order": "A-104", "amount": 41.5},
                      {"order": "A-105", "amount": 63.75},
                      {"order": "A-106", "amount": 5.0},
                      {"order": "A-107", "amount": 28.9},
                  ]}}),
            _node("loop", "loop_over_items", "Loop batches", 260, 0,
                  {"items_path": "items", "batch_size": 2}),
            _node("sum", "code", "Sum batch", 520, -60,
                  {"code": "result = {\n  'batch': input_data['batch']['index'] + 1,\n  'of': input_data['batch']['total'],\n  'orders': len(input_data['items']),\n  'revenue': round(sum(i['amount'] for i in input_data['items']), 2),\n}\n"}),
            _node("totals", "aggregate", "Total revenue", 780, 120,
                  {"mode": "sum", "field": "result.revenue"}),
            _node("report", "set_variable", "Report", 1040, 30,
                  {"keep_input": False, "assignments": {
                      "total_revenue": "{{ nodes.totals.output.value }}",
                      "batches": "{{ nodes.loop.output.done.batches }}",
                      "orders": "{{ nodes.loop.output.done.total_items }}",
                      "note": "Processed {{ nodes.loop.output.done.total_items }} orders in {{ nodes.loop.output.done.batches }} batches",
                  }}),
        ],
        "edges": [
            _edge("e1", "start", "loop"),
            _edge("e2", "loop", "sum", source_handle="loop"),
            _edge("e3", "loop", "totals", source_handle="done"),
            _edge("e4", "totals", "report"),
        ],
    }
    return Workflow(
        name="Batch Orders Digest - loop demo",
        description="Manual trigger → Loop Over Items (batches of 2) → per-batch revenue via Python → Aggregate over done results. Watch the body re-run per batch.",
        graph=graph,
        is_active=True,
    )


async def seed_if_empty(db: AsyncSession) -> None:
    count = (await db.execute(select(func.count()).select_from(Workflow))).scalar_one()
    if count == 0:
        for wf in (_quickstart(), _ai_writer(), _webhook_echo(), _api_ping()):
            db.add(wf)
        logger.info("Seeded %d demo workflows", 4)
    # Top-up: make sure newer demo workflows also exist on older databases.
    for wf in (_batch_digest(),):
        exists = (
            await db.execute(select(func.count()).select_from(Workflow).where(Workflow.name == wf.name))
        ).scalar_one()
        if not exists:
            db.add(wf)
            logger.info("Seeded demo workflow: %s", wf.name)
