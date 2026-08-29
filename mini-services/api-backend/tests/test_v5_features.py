"""V5 feature tests: Wait for Resume (human-in-the-loop) suspend/resume."""

from __future__ import annotations

import asyncio
import time
import uuid

import httpx
import pytest

from app.engine import GraphRunner
from app.engine.nodes.base import BaseNode  # noqa: F401  (registry side-effects)
from app.engine.runner import GraphValidationError, validate_graph_document
from app.engine.schema import GraphSpec
from app.main import app

API = "http://testserver/api/v1"


def run(graph_dict: dict, **kwargs) -> dict:
    graph = GraphSpec.model_validate(graph_dict)
    runner = GraphRunner(graph, workflow_id="wf_test", workflow_name="Test", **kwargs)
    return asyncio.run(runner.run())


WAIT_GRAPH = {
    "nodes": [
        {"id": "t", "type": "manual_trigger", "parameters": {"payload": {"who": "ada"}}},
        {"id": "pre", "type": "set_variable",
         "parameters": {"assignments": {"greet": "hello {{ nodes.t.output.payload.who }}"}, "keep_input": False}},
        {"id": "w", "type": "wait_for_resume", "parameters": {"resume_hint": "approve me"}},
        {"id": "post", "type": "set_variable",
         "parameters": {"assignments": {
             "verdict": "{{ nodes.w.output.approved }}",
             "greet": "{{ nodes.pre.output.greet }}",
             "note": "{{ nodes.w.output.note }}",
         }, "keep_input": False}},
    ],
    "edges": [
        {"id": "e1", "source": "t", "target": "pre"},
        {"id": "e2", "source": "pre", "target": "w"},
        {"id": "e3", "source": "w", "target": "post"},
    ],
}


# ------------------------------------------------------------------ suspend
def test_wait_pauses_before_downstream_and_exposes_resume_info():
    result = run(WAIT_GRAPH)
    assert result["status"] == "waiting", result
    statuses = {r["node_id"]: r["status"] for r in result["node_runs"]}
    assert statuses == {"t": "success", "pre": "success", "w": "waiting"}

    resume = result["resume"]
    assert resume["token"] and resume["node_id"] == "w"
    assert resume["resume_path"] == f"/api/v1/executions/{result['execution_id']}/resume"

    wait_run = next(r for r in result["node_runs"] if r["node_id"] == "w")
    assert wait_run["output"]["paused"] is True
    assert wait_run["output"]["token"] == resume["token"]
    assert wait_run["output"]["resume_hint"] == "approve me"

    # resume_state is complete enough to rebuild the run
    rs = result["resume_state"]
    assert set(rs["node_states"]) == {"t", "pre", "w"}
    assert rs["active_edges"] == ["e1", "e2"]


# ------------------------------------------------------------------ resume
def test_resume_completes_flow_with_payload_and_pre_wait_context():
    first = run(WAIT_GRAPH)
    second = GraphRunner(
        GraphSpec.model_validate(WAIT_GRAPH),
        workflow_id="wf_test", workflow_name="Test", execution_id=first["execution_id"],
        resume_state={
            "node_states": first["resume_state"]["node_states"],
            "active_edges": first["resume_state"]["active_edges"],
            "wait_node_id": first["resume"]["node_id"],
            "wait_output": {"approved": True, "note": "ship it"},
            "prior_node_runs": first["node_runs"],
        },
    )
    result = asyncio.run(second.run())

    assert result["status"] == "success", result["error"]
    statuses = {}
    for r in result["node_runs"]:
        statuses.setdefault(r["node_id"], []).append(r["status"])
    # wait node shows both the pause and the resumed completion
    assert statuses["w"] == ["waiting", "success"]
    assert statuses["post"] == ["success"]

    post = next(r for r in result["node_runs"] if r["node_id"] == "post")
    assert post["output"] == {"verdict": True, "greet": "hello ada", "note": "ship it"}


def test_resume_rejects_downstream_of_inactive_branch_patterns():
    # a wait node that is skipped (no active input) does NOT pause the run
    graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "parameters": {"payload": {"go": False}}},
            {"id": "gate", "type": "if_condition",
             "parameters": {"left_value": "{{ nodes.t.output.payload.go }}", "operator": "is_true"}},
            {"id": "w", "type": "wait_for_resume", "parameters": {}},
            {"id": "end", "type": "set_variable", "parameters": {"assignments": {"a": 1}, "keep_input": False}},
        ],
        "edges": [
            {"id": "e1", "source": "t", "target": "gate"},
            {"id": "e2", "source": "gate", "target": "w", "sourceHandle": "true"},
            {"id": "e3", "source": "gate", "target": "end", "sourceHandle": "false"},
        ],
    }
    result = run(graph)
    assert result["status"] == "success", result
    statuses = {r["node_id"]: r["status"] for r in result["node_runs"]}
    assert statuses["w"] == "skipped"
    assert statuses["end"] == "success"


def test_pass_through_includes_upstream_payload():
    graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "parameters": {"payload": {"order": 42}}},
            {"id": "w", "type": "wait_for_resume", "parameters": {"pass_through": True}},
        ],
        "edges": [{"id": "e1", "source": "t", "target": "w"}],
    }
    result = run(graph)
    wait_run = next(r for r in result["node_runs"] if r["node_id"] == "w")
    # upstream = the manual trigger's full output envelope (payload + metadata)
    assert wait_run["output"]["input"]["payload"] == {"order": 42}


# ------------------------------------------------------- structural guards
def test_wait_node_inside_loop_body_is_rejected_at_validation():
    graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "parameters": {"payload": {"items": [1, 2]}}},
            {"id": "lp", "type": "loop_over_items", "parameters": {"items_path": "items", "batch_size": 1}},
            {"id": "w", "type": "wait_for_resume", "parameters": {}},
        ],
        "edges": [
            {"id": "e1", "source": "t", "target": "lp"},
            {"id": "e2", "source": "lp", "target": "w", "sourceHandle": "loop"},
        ],
    }
    with pytest.raises(GraphValidationError, match="cannot live inside a loop body"):
        validate_graph_document(graph)


# ------------------------------------------------------------- API resume
def test_api_wait_roundtrip_with_token_checks():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=API
        ) as client:
            wf_res = await client.post(
                "/workflows",
                json={
                    "name": f"v5-api-test-{tag}",
                    "description": "temp wait/resume workflow",
                    "is_active": False,
                    "graph": {
                        "nodes": [
                            {"id": "t", "type": "manual_trigger", "parameters": {"payload": {"tag": tag}}},
                            {"id": "w", "type": "wait_for_resume", "parameters": {"resume_hint": "ok?"}},
                            {"id": "post", "type": "set_variable",
                             "parameters": {"assignments": {"answer": "{{ nodes.w.output.answer }}", "tag": "{{ nodes.t.output.payload.tag }}"}, "keep_input": False}},
                        ],
                        "edges": [
                            {"id": "e1", "source": "t", "target": "w"},
                            {"id": "e2", "source": "w", "target": "post"},
                        ],
                    },
                },
            )
            assert wf_res.status_code in (200, 201), wf_res.text
            wf = wf_res.json()
            try:
                acc = await client.post(f"/workflows/{wf['id']}/run", json={})
                exec_id = acc.json()["execution_id"]
                for _ in range(40):
                    res = await client.get(f"/executions/{exec_id}")
                    if res.json()["status"] != "running":
                        break
                    await asyncio.sleep(0.1)
                detail = res.json()
                assert detail["status"] == "waiting", detail
                assert detail["resume"]["url"] == f"/executions/{exec_id}/resume"
                token = detail["resume"]["token"]

                # resume endpoints: 404 unknown, 403 wrong token, 409 non-waiting
                bad = await client.post("/executions/nonexistent/resume", json={"token": "x"})
                assert bad.status_code == 404
                wrong = await client.post(f"/executions/{exec_id}/resume", json={"token": "wrong"})
                assert wrong.status_code == 403
                ok = await client.post(
                    f"/executions/{exec_id}/resume", json={"token": token, "payload": {"answer": 42}}
                )
                assert ok.status_code == 202, ok.text

                for _ in range(60):
                    res = await client.get(f"/executions/{exec_id}")
                    if res.json()["status"] != "running":
                        break
                    await asyncio.sleep(0.1)
                done = res.json()
                assert done["status"] == "success", done
                post = next(n for n in done["node_runs"] if n["node_id"] == "post")
                assert post["output"] == {"answer": 42, "tag": tag}

                # token invalidated: second resume → 409 (no longer waiting)
                again = await client.post(f"/executions/{exec_id}/resume", json={"token": token})
                assert again.status_code == 409

                # rerun of a waiting execution is allowed → runs a fresh copy
                rr = await client.post(f"/executions/{exec_id}/rerun")
                assert rr.status_code == 202
                fresh_id = rr.json()["execution_id"]
                for _ in range(40):
                    res = await client.get(f"/executions/{fresh_id}")
                    if res.json()["status"] != "running":
                        break
                    await asyncio.sleep(0.1)
                assert res.json()["status"] == "waiting"
                await client.delete(f"/executions/{fresh_id}")
            finally:
                await client.delete(f"/workflows/{wf['id']}")
        from app.services import executor as executor_mod

        tasks = [t for t in executor_mod._background_tasks if not t.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(_go())
