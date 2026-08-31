"""V3 feature tests: Loop Over Items engine + integration node dry-runs."""

from __future__ import annotations

import asyncio
import pytest

from app.engine import GraphRunner
from app.engine.nodes.base import BaseNode  # noqa: F401  (registry side-effects)
from app.engine.runner import GraphValidationError, validate_graph_document
from app.engine.schema import GraphSpec


def run(graph_dict: dict, **kwargs) -> dict:
    graph = GraphSpec.model_validate(graph_dict)
    runner = GraphRunner(graph, workflow_id="wf_test", workflow_name="Test", **kwargs)
    return asyncio.run(runner.run())


def _statuses(result: dict) -> dict:
    return {r["node_id"]: r for r in result["node_runs"]}


# ---------------------------------------------------------------------- loop
def test_loop_runs_body_per_batch_and_aggregates():
    graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger",
             "parameters": {"payload": {"items": [{"n": i} for i in range(1, 8)]}}},
            {"id": "lp", "type": "loop_over_items",
             "parameters": {"items_path": "items", "batch_size": 3}},
            {"id": "body", "type": "code",
             "parameters": {"code": "result = {'idx': input_data['batch']['index'], 'count': len(input_data['items']), 'first': input_data['items'][0]['n']}\n"}},
            {"id": "after", "type": "set_variable",
             "parameters": {"keep_input": False, "assignments": {
                 "batches": "{{ nodes.lp.output.done.batches }}",
                 "total_items": "{{ nodes.lp.output.done.total_items }}",
                 "firsts_sum": "{{ nodes.lp.output.done.items | map(attribute='result.first') | sum }}",
             }}},
        ],
        "edges": [
            {"id": "e1", "source": "t", "target": "lp"},
            {"id": "e2", "source": "lp", "target": "body", "sourceHandle": "loop"},
            {"id": "e3", "source": "lp", "target": "after", "sourceHandle": "done"},
        ],
    }
    result = run(graph)
    assert result["status"] == "success", result["error"]
    st = _statuses(result)

    body_runs = [r for r in result["node_runs"] if r["node_id"] == "body"]
    assert [r["batch_index"] for r in body_runs] == [0, 1, 2]
    assert [r["output"]["result"]["count"] for r in body_runs] == [3, 3, 1]
    assert [r["output"]["result"]["first"] for r in body_runs] == [1, 4, 7]

    assert st["after"]["output"]["batches"] == 3
    assert st["after"]["output"]["total_items"] == 7
    assert st["after"]["output"]["firsts_sum"] == 12  # 1 + 4 + 7
    # loop node's own record display = the done payload (raw_output)
    assert st["lp"]["output"]["batches"] == 3


def test_loop_closure_violation_rejected():
    graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "parameters": {}},
            {"id": "side", "type": "set_variable",
             "parameters": {"assignments": {"x": 1}, "keep_input": False}},
            {"id": "lp", "type": "loop_over_items", "parameters": {}},
            {"id": "body", "type": "set_variable",
             "parameters": {"assignments": {"y": 2}, "keep_input": False}},
        ],
        "edges": [
            {"id": "e1", "source": "t", "target": "lp"},
            {"id": "e2", "source": "lp", "target": "body", "sourceHandle": "loop"},
            {"id": "e3", "source": "side", "target": "body"},
        ],
    }
    # save-time: 400 via API
    with pytest.raises(GraphValidationError, match="outside the loop body"):
        validate_graph_document(graph)
    # run-time: execution fails cleanly instead of raising
    result = run(graph)
    assert result["status"] == "error"
    assert "outside the loop body" in result["error"]


def test_loop_body_inherits_preloop_outputs():
    graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger",
             "parameters": {"payload": {"items": [1, 2, 3, 4]}}},
            {"id": "prep", "type": "set_variable",
             "parameters": {"assignments": {"tenant": "acme"}, "keep_input": False}},
            {"id": "lp", "type": "loop_over_items",
             "parameters": {"items_path": "items", "batch_size": 2}},
            {"id": "body", "type": "set_variable",
             "parameters": {"assignments": {
                 "tenant": "{{ nodes.prep.output.tenant }}",
                 "chunk": "{{ input.items }}",
             }, "keep_input": False}},
        ],
        "edges": [
            {"id": "e1", "source": "t", "target": "prep"},
            {"id": "e2", "source": "t", "target": "lp"},
            {"id": "e3", "source": "lp", "target": "body", "sourceHandle": "loop"},
        ],
    }
    result = run(graph)
    assert result["status"] == "success", result["error"]
    done = _statuses(result)["lp"]["output"]
    assert done["items"] == [{"tenant": "acme", "chunk": [1, 2]}, {"tenant": "acme", "chunk": [3, 4]}]


def test_nested_loops():
    graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger",
             "parameters": {"payload": {"groups": [{"items": [1, 2, 3]}, {"items": [10, 20]}]}}},
            {"id": "outer", "type": "loop_over_items",
             "parameters": {"items_path": "groups", "batch_size": 1}},
            {"id": "inner", "type": "loop_over_items",
             "parameters": {"items_path": "items", "batch_size": 1}},
            {"id": "ibody", "type": "code",
             "parameters": {"code": "result = {'nums': input_data['items'][0]['items']}\n"}},
        ],
        "edges": [
            {"id": "e1", "source": "t", "target": "outer"},
            {"id": "e2", "source": "outer", "target": "inner", "sourceHandle": "loop"},
            {"id": "e3", "source": "inner", "target": "ibody", "sourceHandle": "loop"},
        ],
    }
    result = run(graph)
    assert result["status"] == "success", result["error"]
    st = _statuses(result)
    done = st["outer"]["output"]
    assert done["batches"] == 2
    assert done["items"] == [
        {"result": {"nums": [1, 2, 3]}},
        {"result": {"nums": [10, 20]}},
    ]
    assert st["ibody"]["status"] == "success"


def test_empty_items_yields_zero_batches():
    graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "parameters": {"payload": {"items": []}}},
            {"id": "lp", "type": "loop_over_items", "parameters": {"items_path": "items"}},
            {"id": "body", "type": "set_variable",
             "parameters": {"assignments": {"x": 1}, "keep_input": False}},
        ],
        "edges": [
            {"id": "e1", "source": "t", "target": "lp"},
            {"id": "e2", "source": "lp", "target": "body", "sourceHandle": "loop"},
        ],
    }
    result = run(graph)
    assert result["status"] == "success", result["error"]
    done = _statuses(result)["lp"]["output"]
    assert done["batches"] == 0 and done["items"] == []


# --------------------------------------------------------------- integrations
def test_email_dry_run_preview():
    graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger",
             "parameters": {"payload": {"customer": "Ada", "total": 42}}},
            {"id": "mail", "type": "email_send",
             "parameters": {
                 "to": "ada@example.com, bob@example.com",
                 "subject": "Order confirmed",
                 "body": "Thanks {{ nodes.t.output.payload.customer }} - total {{ nodes.t.output.payload.total }} EUR",
                 "dry_run": True,
             }},
        ],
        "edges": [{"id": "e", "source": "t", "target": "mail"}],
    }
    result = run(graph)
    assert result["status"] == "success", result["error"]
    out = _statuses(result)["mail"]["output"]
    assert out["delivered"] is False and out["dry_run"] is True
    assert out["message"]["to"] == ["ada@example.com", "bob@example.com"]
    assert "Ada" in out["message"]["body"] and "42" in out["message"]["body"]


def test_email_send_requires_credential_when_not_dry_run():
    graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "parameters": {}},
            {"id": "mail", "type": "email_send",
             "parameters": {"to": "a@b.c", "subject": "s", "body": "b", "dry_run": False}},
        ],
        "edges": [{"id": "e", "source": "t", "target": "mail"}],
    }
    result = run(graph)
    st = _statuses(result)["mail"]
    assert st["status"] == "error"
    assert "SMTP credential" in (st["error"] or "")


def test_slack_dry_run_webhook_mode():
    graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger",
             "parameters": {"payload": {"customer": "Grace"}}},
            {"id": "alert", "type": "slack_message",
             "parameters": {
                 "webhook_url": "https://hooks.slack.com/services/T000/B000/XXXX",
                 "text": "New order from {{ nodes.t.output.payload.customer }}",
                 "dry_run": True,
             }},
        ],
        "edges": [{"id": "e", "source": "t", "target": "alert"}],
    }
    result = run(graph)
    assert result["status"] == "success", result["error"]
    out = _statuses(result)["alert"]["output"]
    assert out["delivered"] is False and out["mode"] == "webhook"
    assert out["payload"]["text"] == "New order from Grace"


def test_slack_requires_target():
    graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "parameters": {}},
            {"id": "alert", "type": "slack_message",
             "parameters": {"text": "hi", "dry_run": False}},
        ],
        "edges": [{"id": "e", "source": "t", "target": "alert"}],
    }
    result = run(graph)
    st = _statuses(result)["alert"]
    assert st["status"] == "error"
    assert "webhook_url" in (st["error"] or "")
