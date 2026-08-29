"""Phase 1 milestone demo — standalone graph execution.

Runs a mock JSON graph of three dummy nodes in correct dependency order using
graphlib.TopologicalSorter + the Jinja2 templating layer. No database, no API —
pure engine, exactly like the roadmap milestone describes:

    python demo/phase1_demo.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # allow `python demo/...`

from app.engine import GraphRunner  # noqa: E402
from app.engine.schema import GraphSpec  # noqa: E402

MOCK_GRAPH = {
    "nodes": [
        {
            "id": "trigger_1",
            "type": "manual_trigger",
            "name": "Start",
            "position": {"x": 0, "y": 0},
            "parameters": {"payload": {"user": "Ada", "id": 42}},
        },
        {
            "id": "set_1",
            "type": "set_variable",
            "name": "Build greeting",
            "position": {"x": 220, "y": 0},
            "parameters": {
                "keep_input": False,
                "assignments": {
                    "greeting": "Hello {{ nodes.trigger_1.output.payload.user }}!",
                    "user_id": "{{ nodes.trigger_1.output.payload.id }}",
                },
            },
        },
        {
            "id": "code_1",
            "type": "code",
            "name": "Summarize",
            "position": {"x": 440, "y": 0},
            "parameters": {
                "code": (
                    "name = input_data['greeting']\n"
                    "result = {'summary': name + ' (id=' + str(input_data['user_id']) + ')'}\n"
                )
            },
        },
    ],
    "edges": [
        {"id": "e1", "source": "trigger_1", "target": "set_1"},
        {"id": "e2", "source": "set_1", "target": "code_1"},
    ],
}


def log_event(event: dict) -> None:
    label = event["event"]
    node = event.get("node_name", "")
    status = event.get("status", "")
    print(f"  [{label:<20}] {node:<16} {status}")


async def main() -> None:
    print("=" * 62)
    print("Py8n Phase 1 — standalone graph execution demo")
    print("=" * 62)

    graph = GraphSpec.model_validate(MOCK_GRAPH)
    print(f"Validated graph: {len(graph.nodes)} nodes, {len(graph.edges)} edges")

    runner = GraphRunner(
        graph,
        workflow_id="demo-workflow",
        workflow_name="Phase 1 Demo",
        trigger_type="manual",
        emit=log_event,
    )
    result = await runner.run()

    print("-" * 62)
    print(f"Execution order observed : {' -> '.join(r['node_id'] for r in result['node_runs'])}")
    print(f"Final status             : {result['status']} in {result['duration_ms']} ms")
    print("Final node output        :")
    print(json.dumps(result["node_runs"][-1]["output"], indent=2, ensure_ascii=False))

    expected = ["trigger_1", "set_1", "code_1"]
    actual = [r["node_id"] for r in result["node_runs"]]
    assert actual == expected, f"Wrong order: {actual}"
    assert result["status"] == "success"
    print("-" * 62)
    print("PASS — three nodes executed in correct topological order.")


if __name__ == "__main__":
    asyncio.run(main())
