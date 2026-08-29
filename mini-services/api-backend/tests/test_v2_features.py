"""V2 feature tests: data-flow nodes, retry/continue-on-fail settings."""

from __future__ import annotations

import asyncio

from app.engine import GraphRunner
from app.engine.schema import GraphSpec


def run(graph_dict: dict, **kwargs) -> dict:
    graph = GraphSpec.model_validate(graph_dict)
    runner = GraphRunner(graph, workflow_id="wf_test", workflow_name="Test", **kwargs)
    return asyncio.run(runner.run())


def _statuses(result: dict) -> dict:
    return {r["node_id"]: r for r in result["node_runs"]}


# ---------------------------------------------------------------- filter
def test_filter_keeps_matching_items():
    graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "parameters": {"payload": {"items": [
                {"name": "ada", "score": 90}, {"name": "bob", "score": 30}, {"name": "cy", "score": 75},
            ]}}},
            {
                "id": "f", "type": "filter",
                "parameters": {"field": "score", "operator": "greater_than", "right_value": 50},
            },
        ],
        "edges": [{"id": "e", "source": "t", "target": "f"}],
    }
    result = run(graph)
    out = _statuses(result)["f"]["output"]
    assert [i["name"] for i in out["items"]] == ["ada", "cy"]
    assert out["matched"] == 2 and out["dropped"] == 1
    assert result["status"] == "success"


# ---------------------------------------------------------------- switch
def test_switch_routes_by_rule_and_fallback():
    rules = ["urgent", "normal", "low"]
    base = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "parameters": {}},
            {
                "id": "sw", "type": "switch",
                "parameters": {"field": "priority", "rules": rules, "use_fallback": True},
            },
            {"id": "r0", "type": "set_variable", "parameters": {"assignments": {"lane": "urgent"}, "keep_input": False}},
            {"id": "fb", "type": "set_variable", "parameters": {"assignments": {"lane": "fallback"}, "keep_input": False}},
        ],
        "edges": [
            {"id": "e", "source": "t", "target": "sw"},
            {"id": "e0", "source": "sw", "target": "r0", "sourceHandle": "0"},
            {"id": "ef", "source": "sw", "target": "fb", "sourceHandle": "fallback"},
        ],
    }

    result = run(_with_payload(base, {"priority": "urgent"}))
    st = _statuses(result)
    assert st["r0"]["status"] == "success" and st["fb"]["status"] == "skipped"

    result = run(_with_payload(base, {"priority": "whatever"}))
    st = _statuses(result)
    assert st["r0"]["status"] == "skipped" and st["fb"]["status"] == "success"


def _with_payload(base: dict, payload: dict) -> dict:
    g = {**base, "nodes": [dict(n) for n in base["nodes"]]}
    g["nodes"][0] = {**g["nodes"][0], "parameters": {"payload": payload}}
    return g


# ----------------------------------------------------------------- merge
def test_merge_append_and_combine():
    graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "parameters": {"payload": {"items": [1]}}},
            {"id": "a", "type": "set_variable", "parameters": {"assignments": {"x": 1}, "keep_input": False}},
            {"id": "b", "type": "set_variable", "parameters": {"assignments": {"y": 2}, "keep_input": False}},
            {"id": "m", "type": "merge", "parameters": {"mode": "combine"}},
            {"id": "out", "type": "set_variable", "parameters": {"assignments": {"got": "{{ nodes.m.output }}"}, "keep_input": False}},
        ],
        "edges": [
            {"id": "e0", "source": "t", "target": "a"},
            {"id": "e1", "source": "t", "target": "b"},
            {"id": "e2", "source": "a", "target": "m"},
            {"id": "e3", "source": "b", "target": "m"},
            {"id": "e4", "source": "m", "target": "out"},
        ],
    }
    result = run(graph)
    out = _statuses(result)["out"]["output"]
    assert out["got"]["x"] == 1 and out["got"]["y"] == 2


# -------------------------------------------------------- split + aggregate
def test_split_out_then_aggregate_sum():
    graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "parameters": {"payload": {"data": {"results": [
                {"amt": 10}, {"amt": 20}, {"amt": 32},
            ]}}}},
            {"id": "s", "type": "split_out", "parameters": {"field": "data.results"}},
            {"id": "agg", "type": "aggregate", "parameters": {"mode": "sum", "field": "amt"}},
        ],
        "edges": [
            {"id": "e0", "source": "t", "target": "s"},
            {"id": "e1", "source": "s", "target": "agg"},
        ],
    }
    result = run(graph)
    out = _statuses(result)["agg"]["output"]
    assert out["value"] == 62 and out["count"] == 3


def test_aggregate_join():
    graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "parameters": {"payload": {"items": [
                {"n": "ada"}, {"n": "bob"},
            ]}}},
            {"id": "agg", "type": "aggregate", "parameters": {"mode": "join", "field": "n", "separator": " & "}},
        ],
        "edges": [{"id": "e", "source": "t", "target": "agg"}],
    }
    result = run(graph)
    assert _statuses(result)["agg"]["output"]["value"] == "ada & bob"


# --------------------------------------------------------- retry settings
def test_retry_on_fail_records_attempts():
    graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "parameters": {}},
            {"id": "boom", "type": "code",
             "parameters": {"code": "result = 1/0"},
             "settings": {"retry_on_fail": True, "max_retries": 2, "retry_wait_ms": 1}},
        ],
        "edges": [{"id": "e", "source": "t", "target": "boom"}],
    }
    result = run(graph)
    rec = _statuses(result)["boom"]
    assert rec["status"] == "error" and rec.get("attempts") == 3
    assert result["status"] == "error"


def test_continue_on_fail_keeps_flow_alive():
    graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "parameters": {}},
            {"id": "boom", "type": "code", "name": "Boom Machine",
             "parameters": {"code": "result = 1/0"},
             "settings": {"continue_on_fail": True}},
            {"id": "after", "type": "set_variable",
             "parameters": {"assignments": {"src": "{{ nodes.boom.output.failed_node }}", "kind": "{{ nodes.boom.output.error | string | truncate(64) }}"}, "keep_input": False}},
        ],
        "edges": [{"id": "e0", "source": "t", "target": "boom"}, {"id": "e1", "source": "boom", "target": "after"}],
    }
    result = run(graph)
    st = _statuses(result)
    assert st["boom"]["status"] == "error"
    assert st["boom"].get("continued_on_fail") is True
    assert st["after"]["status"] == "success"
    assert st["after"]["output"]["src"] == "Boom Machine"
    assert "ZeroDivisionError" in st["after"]["output"]["kind"]
    assert result["status"] == "success"  # run survives the handled failure
