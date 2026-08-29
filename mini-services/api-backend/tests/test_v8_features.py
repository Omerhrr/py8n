"""V8 feature tests: node disable, execution cancel, error-workflow routing."""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest

from app.engine import GraphRunner
from app.engine.nodes.base import BaseNode  # noqa: F401  (registry side-effects)
from app.engine.runner import GraphValidationError
from app.engine.schema import GraphSpec
from app.main import app
from app.services import executor as executor_mod

API = "http://testserver/api/v1"


def run(graph_dict: dict, **kwargs) -> dict:
    graph = GraphSpec.model_validate(graph_dict)
    runner = GraphRunner(graph, workflow_id="wf_test", workflow_name="Test", **kwargs)
    return asyncio.run(runner.run())


async def _drain_background() -> None:
    """Await every in-flight background execution task (and their spawns)."""
    for _ in range(20):
        tasks = set(executor_mod._background_tasks)
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)


# --------------------------------------------------------------- disable
def test_disabled_node_passes_input_through():
    graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "parameters": {"payload": {"tag": "keepme"}}},
            {"id": "off", "type": "code", "name": "Off Duty", "parameters": {"code": "result = 1/0"},
             "disabled": True},
            {"id": "after", "type": "set_variable",
             "parameters": {"assignments": {
                 "tag": "{{ nodes.off.output.payload.tag }}",
             }, "keep_input": False}},
        ],
        "edges": [
            {"id": "e1", "source": "t", "target": "off"},
            {"id": "e2", "source": "off", "target": "after"},
        ],
    }
    result = run(graph)
    statuses = {r["node_id"]: r for r in result["node_runs"]}
    assert result["status"] == "success", result
    # the disabled node never executed its code (1/0 would explode)
    assert statuses["off"]["status"] == "skipped"
    assert "disabled" in (statuses["off"].get("error") or "")
    # pass-through: downstream sees the trigger payload through the disabled node
    assert statuses["after"]["status"] == "success"
    assert statuses["after"]["output"] == {"tag": "keepme"}


def test_all_triggers_disabled_is_rejected():
    graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "parameters": {}, "disabled": True},
        ],
        "edges": [],
    }
    with pytest.raises(GraphValidationError, match="All trigger nodes are disabled"):
        run(graph)


# ---------------------------------------------------------------- cancel
def test_cancel_event_stops_runner_between_nodes():
    graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "parameters": {"payload": {"x": 1}}},
            {"id": "d", "type": "delay", "parameters": {"seconds": 1}},
            {"id": "after", "type": "set_variable", "parameters": {"assignments": {"a": "1"}, "keep_input": False}},
        ],
        "edges": [
            {"id": "e1", "source": "t", "target": "d"},
            {"id": "e2", "source": "d", "target": "after"},
        ],
    }

    async def _go():
        cancel_event = asyncio.Event()
        runner = GraphRunner(
            GraphSpec.model_validate(graph),
            workflow_id="wf_test", workflow_name="Test", cancel_event=cancel_event,
        )
        task = asyncio.get_running_loop().create_task(runner.run())
        await asyncio.sleep(0.15)  # let trigger + delay start
        cancel_event.set()
        return await task

    result = asyncio.run(_go())
    statuses = {r["node_id"]: r["status"] for r in result["node_runs"]}
    assert result["status"] == "cancelled"
    assert statuses["t"] == "success"
    assert statuses["d"] == "success"  # the node in flight was allowed to finish
    assert "after" not in statuses     # the next node never ran


# ------------------------------------------------- error-workflow routing
async def _make_workflow(client: httpx.AsyncClient, name: str, graph: dict, handler_id=None) -> dict:
    res = await client.post(
        "/workflows",
        json={"name": name, "description": "v8 temp", "is_active": False, "graph": graph,
              "error_workflow_id": handler_id},
    )
    assert res.status_code in (200, 201), res.text
    return res.json()


async def _delete_workflow(client: httpx.AsyncClient, wf_id: str) -> None:
    await client.delete(f"/workflows/{wf_id}")


def test_error_workflow_dispatched_with_structured_payload():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=API
        ) as client:
            handler = await _make_workflow(
                client, f"v8-handler-{tag}",
                {
                    "nodes": [
                        {"id": "t", "type": "manual_trigger", "parameters": {}},
                        {"id": "grab", "type": "set_variable",
                         "parameters": {"assignments": {
                             "src": "{{ execution.trigger_payload.workflow_name }}",
                             "err": "{{ execution.trigger_payload.error | string | truncate(120) }}",
                             "origin": "{{ execution.trigger_payload.execution_id }}",
                         }, "keep_input": False}},
                    ],
                    "edges": [{"id": "e1", "source": "t", "target": "grab"}],
                },
            )
            failing = await _make_workflow(
                client, f"v8-failing-{tag}",
                {
                    "nodes": [
                        {"id": "t", "type": "manual_trigger", "parameters": {}},
                        {"id": "boom", "type": "code", "parameters": {"code": "result = 1/0"}},
                    ],
                    "edges": [{"id": "e1", "source": "t", "target": "boom"}],
                },
                handler_id=handler["id"],
            )
            try:
                from app.services.executor import execute_workflow
                from app.db import AsyncSessionLocal
                from app.models import ExecutionLog

                result = await execute_workflow(failing["id"], trigger_type="manual",
                                                trigger_payload={})
                assert result["status"] == "error"
                handler_exec = result.get("error_workflow_execution_id")
                assert handler_exec, "error workflow was not dispatched"

                await _drain_background()
                async with AsyncSessionLocal() as session:
                    row = await session.get(ExecutionLog, handler_exec)
                assert row is not None and row.status == "success", row
                assert row.trigger_type == "error"
                assert row.trigger_payload["workflow_name"] == failing["name"]
                assert row.trigger_payload["execution_id"] == result["execution_id"]
                assert "ZeroDivision" in row.trigger_payload["error"]
                assert row.trigger_payload["failed_nodes"][0]["node_id"] == "boom"

                # handler output proves the payload reached the graph
                grab = next(r for r in row.node_runs if r["node_id"] == "grab")
                assert grab["output"]["src"] == failing["name"]
                assert grab["output"]["origin"] == result["execution_id"]
            finally:
                await _delete_workflow(client, failing["id"])
                await _delete_workflow(client, handler["id"])
                await _drain_background()

    asyncio.run(_go())


def test_error_run_does_not_recurse():
    """A failing error-handler run must not spawn further error executions."""
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=API
        ) as client:
            handler = await _make_workflow(
                client, f"v8-broken-handler-{tag}",
                {
                    "nodes": [
                        {"id": "t", "type": "manual_trigger", "parameters": {}},
                        {"id": "boom", "type": "code", "parameters": {"code": "result = 1/0"}},
                    ],
                    "edges": [{"id": "e1", "source": "t", "target": "boom"}],
                },
            )
            failing = await _make_workflow(
                client, f"v8-failing2-{tag}",
                {
                    "nodes": [
                        {"id": "t", "type": "manual_trigger", "parameters": {}},
                        {"id": "boom", "type": "code", "parameters": {"code": "result = 1/0"}},
                    ],
                    "edges": [{"id": "e1", "source": "t", "target": "boom"}],
                },
                handler_id=handler["id"],
            )
            try:
                from app.services.executor import execute_workflow
                from app.db import AsyncSessionLocal
                from sqlalchemy import select
                from app.models import ExecutionLog

                result = await execute_workflow(failing["id"], trigger_type="manual",
                                                trigger_payload={})
                assert result["status"] == "error"
                await _drain_background()

                async with AsyncSessionLocal() as session:
                    rows = (
                        await session.execute(
                            select(ExecutionLog).where(
                                ExecutionLog.trigger_type == "error",
                                ExecutionLog.workflow_id == handler["id"],
                            )
                        )
                    ).scalars().all()
                # exactly ONE error-triggered run of the handler, and it failed
                # without spawning another error execution (trigger_type guard).
                assert len(rows) == 1, rows
                assert rows[0].status == "error"
            finally:
                await _delete_workflow(client, failing["id"])
                await _delete_workflow(client, handler["id"])
                await _drain_background()

    asyncio.run(_go())


# -------------------------------------------------------------- API tests
def test_api_cancel_roundtrip_and_guards():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=API
        ) as client:
            wf = await _make_workflow(
                client, f"v8-cancel-{tag}",
                {
                    "nodes": [
                        {"id": "t", "type": "manual_trigger", "parameters": {}},
                        {"id": "d", "type": "delay", "parameters": {"seconds": 4}},
                        {"id": "after", "type": "set_variable",
                         "parameters": {"assignments": {"done": "yes"}, "keep_input": False}},
                    ],
                    "edges": [
                        {"id": "e1", "source": "t", "target": "d"},
                        {"id": "e2", "source": "d", "target": "after"},
                    ],
                },
            )
            try:
                acc = await client.post(f"/workflows/{wf['id']}/run", json={})
                exec_id = acc.json()["execution_id"]
                await asyncio.sleep(0.3)  # let it enter the 4s delay

                res = await client.post(f"/executions/{exec_id}/cancel")
                assert res.status_code == 202, res.text

                for _ in range(40):
                    detail = (await client.get(f"/executions/{exec_id}")).json()
                    if detail["status"] != "running":
                        break
                    await asyncio.sleep(0.1)
                assert detail["status"] == "cancelled", detail
                assert "Cancelled" in (detail["error"] or "")

                # cancelling a finished/unknown execution → 409 / 404
                again = await client.post(f"/executions/{exec_id}/cancel")
                assert again.status_code == 409
                missing = await client.post("/executions/nonexistent/cancel")
                assert missing.status_code == 404
            finally:
                await _delete_workflow(client, wf["id"])
                await _drain_background()

    asyncio.run(_go())


def test_api_error_workflow_binding_validation():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=API
        ) as client:
            handler = await _make_workflow(client, f"v8-bind-handler-{tag}",
                                           {"nodes": [{"id": "t", "type": "manual_trigger",
                                                       "parameters": {}}], "edges": []})
            wf = None
            try:
                # unknown handler → 400
                bad = await client.post("/workflows", json={
                    "name": f"v8-bind-bad-{tag}", "graph": {"nodes": [], "edges": []},
                    "error_workflow_id": "no-such-id",
                })
                assert bad.status_code == 400

                wf = await _make_workflow(
                    client, f"v8-bind-ok-{tag}",
                    {"nodes": [{"id": "t", "type": "manual_trigger", "parameters": {}}], "edges": []},
                    handler_id=handler["id"],
                )
                assert wf["error_workflow_id"] == handler["id"]

                # self-binding → 400
                self_bind = await client.put(
                    f"/workflows/{wf['id']}", json={"error_workflow_id": wf["id"]}
                )
                assert self_bind.status_code == 400

                # listing resolves the handler name
                listed = (await client.get("/workflows")).json()
                entry = next(w for w in listed if w["id"] == wf["id"])
                assert entry["error_workflow_name"] == handler["name"]

                # clear with ""
                cleared = await client.put(f"/workflows/{wf['id']}", json={"error_workflow_id": ""})
                assert cleared.status_code == 200
                assert cleared.json()["error_workflow_id"] is None

                # clearing an unbound field must not touch other bindings (tri-state)
                untouched = await client.put(
                    f"/workflows/{wf['id']}", json={"description": "still unbound"}
                )
                assert untouched.status_code == 200
                assert untouched.json()["error_workflow_id"] is None
                assert untouched.json()["description"] == "still unbound"
            finally:
                await _delete_workflow(client, handler["id"])
                if wf is not None:
                    await _delete_workflow(client, wf["id"])

    asyncio.run(_go())
