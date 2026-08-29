"""Engine unit tests: topo order, Jinja resolution, IF branching, cycle detection."""

from __future__ import annotations

import asyncio

import pytest

from app.engine import GraphRunner
from app.engine.schema import GraphSpec
from app.engine.templating import TemplateResolutionError, resolve_value


def run(graph_dict: dict, **kwargs) -> dict:
    graph = GraphSpec.model_validate(graph_dict)
    runner = GraphRunner(graph, workflow_id="wf_test", workflow_name="Test", **kwargs)
    return asyncio.run(runner.run())


def test_topological_order():
    graph = {
        "nodes": [
            {"id": "a", "type": "manual_trigger", "parameters": {}},
            {"id": "b", "type": "set_variable", "parameters": {"assignments": {"x": 1}, "keep_input": False}},
            {"id": "c", "type": "set_variable", "parameters": {"assignments": {"y": 2}, "keep_input": False}},
        ],
        "edges": [{"id": "e1", "source": "a", "target": "c"}, {"id": "e2", "source": "c", "target": "b"}],
    }
    result = run(graph)
    order = [r["node_id"] for r in result["node_runs"]]
    assert order.index("a") < order.index("c") < order.index("b")
    assert result["status"] == "success"


def test_jinja_resolution_types():
    ctx = {"nodes": {"n1": {"status": "success", "output": {"payload": {"id": 7, "name": "ada"}}}}}
    assert resolve_value("{{ nodes.n1.output.payload.id }}", ctx) == 7
    assert resolve_value("Hello {{ nodes.n1.output.payload.name | upper }}", ctx) == "Hello ADA"
    with pytest.raises(TemplateResolutionError):
        resolve_value("{{ nodes.ghost.output.x }}", ctx)


def test_if_branch_skips_inactive():
    graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "parameters": {"payload": {"age": 30}}},
            {"id": "if1", "type": "if_condition", "parameters": {"left_value": "{{ nodes.t.output.payload.age }}", "operator": "greater_than", "right_value": 18}},
            {"id": "yes", "type": "set_variable", "parameters": {"assignments": {"branch": "adult"}, "keep_input": False}},
            {"id": "no", "type": "set_variable", "parameters": {"assignments": {"branch": "minor"}, "keep_input": False}},
        ],
        "edges": [
            {"id": "e0", "source": "t", "target": "if1"},
            {"id": "e1", "source": "if1", "target": "yes", "sourceHandle": "true"},
            {"id": "e2", "source": "if1", "target": "no", "sourceHandle": "false"},
        ],
    }
    result = run(graph)
    statuses = {r["node_id"]: r["status"] for r in result["node_runs"]}
    assert statuses["yes"] == "success"
    assert statuses["no"] == "skipped"
    assert result["status"] == "success"


def test_cycle_rejected():
    graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "parameters": {}},
            {"id": "a", "type": "set_variable", "parameters": {}},
            {"id": "b", "type": "set_variable", "parameters": {}},
        ],
        "edges": [
            {"id": "e1", "source": "t", "target": "a"},
            {"id": "e2", "source": "a", "target": "b"},
            {"id": "e3", "source": "b", "target": "a"},
        ],
    }
    with pytest.raises(Exception):
        run(graph)


def test_code_node_and_error_propagation():
    graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "parameters": {}},
            {"id": "c1", "type": "code", "parameters": {"code": "result = {'n': 1 + 1}"}},
            {"id": "boom", "type": "code", "parameters": {"code": "result = 1/0"}},
        ],
        "edges": [{"id": "e1", "source": "t", "target": "c1"}, {"id": "e2", "source": "c1", "target": "boom"}],
    }
    result = run(graph)
    statuses = {r["node_id"]: r["status"] for r in result["node_runs"]}
    assert statuses["c1"] == "success"
    assert statuses["boom"] == "error"
    assert result["status"] == "error"
    assert "ZeroDivisionError" in (result["error"] or "")
