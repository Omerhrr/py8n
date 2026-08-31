"""V22 feature tests: Error Trigger, Stop and Error, Sort / Limit / Remove Duplicates.

Covers the completed error-handling story: a workflow bound to an error handler
runs the handler via the dedicated error_trigger node (selected by the runner's
trigger_type="error" mapping) with the structured error payload, plus the Stop
and Error node that fails runs deliberately with a Jinja-resolved message, and
the data-ops trio (sort / limit / remove duplicates) over the items array.

Runs the FastAPI app in-process via httpx ASGITransport (same harness as v4-v21).
"""

from __future__ import annotations

import asyncio
import uuid

import httpx

from app.main import app
from app.services import executor as executor_mod

API = "http://testserver/api/v1"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _drain_background() -> None:
    tasks = [t for t in executor_mod._background_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _cleanup(workflow_ids: list[str]) -> None:
    async with _client() as client:
        for wid in workflow_ids:
            try:
                await client.delete(f"/workflows/{wid}")
            except Exception:
                pass
    await _drain_background()


def _node(nid: str, ntype: str, params: dict | None = None, name: str | None = None) -> dict:
    return {
        "id": nid,
        "type": ntype,
        "name": name or nid,
        "position": {"x": 0, "y": 0},
        "parameters": params or {},
    }


async def _make_workflow(client: httpx.AsyncClient, name: str, graph: dict) -> str:
    res = await client.post("/workflows", json={"name": name, "graph": graph})
    assert res.status_code == 201, res.text
    return res.json()["id"]


async def _run_and_wait(client: httpx.AsyncClient, workflow_id: str, payload: dict | None = None) -> dict:
    res = await client.post(f"/workflows/{workflow_id}/run", json={"payload": payload or {}})
    assert res.status_code in (200, 202), res.text
    exec_id = res.json()["execution_id"]
    for _ in range(100):
        res = await client.get(f"/executions/{exec_id}")
        assert res.status_code == 200, res.text
        if res.json()["status"] != "running":
            return res.json()
        await asyncio.sleep(0.05)
    raise AssertionError("execution did not finish in time")


def _find_node_run(execution: dict, node_name: str) -> dict | None:
    for run in execution.get("node_runs") or []:
        if run.get("node_name") == node_name:
            return run
    return None


# ---------------------------------------------------------------------------
# 1) Error workflow E2E: failing workflow -> handler with Error Trigger
# ---------------------------------------------------------------------------
def test_v22_error_workflow_with_error_trigger():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []

    async def _go():
        async with _client() as client:
            # Handler: error trigger -> set variable embedding payload fields
            handler_graph = {
                "nodes": [
                    _node("et", "error_trigger"),
                    _node(
                        "alert",
                        "set_variable",
                        {
                            "assignments": {
                                "message": "WF {{ nodes.et.output.workflow_name }} failed: {{ nodes.et.output.error }}"
                            }
                        },
                        "Alert",
                    ),
                ],
                "edges": [
                    {"id": "e1", "source": "et", "target": "alert", "sourceHandle": "main", "targetHandle": "main"}
                ],
            }
            handler_id = await _make_workflow(client, f"tmp v22 handler {tag}", handler_graph)
            wf_ids.append(handler_id)

            # Main: manual trigger -> stop and error with a templated message
            main_graph = {
                "nodes": [
                    _node("t", "manual_trigger", {"payload": {"order_id": "ORD-77"}}),
                    _node(
                        "boom",
                        "stop_and_error",
                        {"error_message": "Order {{ nodes.t.output.payload.order_id }} is invalid", "error_type": "ValidationError"},
                        "Validate Order",
                    ),
                ],
                "edges": [{"id": "e2", "source": "t", "target": "boom", "sourceHandle": "main", "targetHandle": "main"}],
            }
            main_id = await _make_workflow(client, f"tmp v22 main {tag}", main_graph)
            wf_ids.append(main_id)

            # Bind the handler
            res = await client.put(f"/workflows/{main_id}", json={"error_workflow_id": handler_id})
            assert res.status_code == 200, res.text
            assert res.json()["error_workflow_id"] == handler_id

            # Run the main workflow -> deliberate failure
            result = await _run_and_wait(client, main_id)
            assert result["status"] == "error", result
            boom_run = _find_node_run(result, "Validate Order")
            assert boom_run is not None and boom_run["status"] == "error"
            assert "Order ORD-77 is invalid" in (boom_run.get("error") or "")

            # Handler must have run with trigger_type=error and produced the alert
            res = await client.get("/executions", params={"workflow_id": handler_id, "limit": 10})
            assert res.status_code == 200, res.text
            handler_runs = [e for e in res.json() if e["trigger_type"] == "error"]
            assert handler_runs, f"no error-triggered handler run in {res.json()}"
            # dispatch_inline runs the handler as a background task - wait for it
            for _ in range(100):
                h = (await client.get(f"/executions/{handler_runs[0]['id']}")).json()
                if h["status"] != "running":
                    break
                await asyncio.sleep(0.05)
            assert h["status"] == "success", h.get("error")
            alert_run = _find_node_run(h, "Alert")
            assert alert_run is not None, h.get("node_runs")
            msg = str(alert_run.get("output"))
            assert "tmp v22 main" in msg  # workflow_name
            assert "Order ORD-77 is invalid" in msg  # main run's error

            # error trigger output carries the structured payload
            et_run = _find_node_run(h, "et")
            assert et_run is not None
            out_payload = et_run.get("output") or {}
            assert out_payload.get("trigger_type") == "error"
            # dispatched error = run-level wrapped message; node error is its tail
            assert boom_run.get("error") in (out_payload.get("error") or "")
            assert out_payload.get("workflow_id") == main_id
            failed = out_payload.get("failed_nodes")
            assert isinstance(failed, list) and failed and failed[0]["node_name"] == "Validate Order"

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids))


# ---------------------------------------------------------------------------
# 2) Stop and Error: message resolution + downstream skipped + no dispatch
# ---------------------------------------------------------------------------
def test_v22_stop_and_error_semantics():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []

    async def _go():
        async with _client() as client:
            graph = {
                "nodes": [
                    _node("t", "manual_trigger", {"payload": {"stock": 0}}),
                    _node(
                        "halt",
                        "stop_and_error",
                        {"error_message": "Out of stock: {{ nodes.t.output.payload.stock }}", "error_type": "OutOfStock"},
                        "Halt",
                    ),
                    _node("never", "set_variable", {"assignments": {"x": "1"}}, "Never"),
                ],
                "edges": [
                    {"id": "e1", "source": "t", "target": "halt", "sourceHandle": "main", "targetHandle": "main"},
                    {"id": "e2", "source": "halt", "target": "never", "sourceHandle": "main", "targetHandle": "main"},
                ],
            }
            wf_id = await _make_workflow(client, f"tmp v22 halt {tag}", graph)
            wf_ids.append(wf_id)

            result = await _run_and_wait(client, wf_id)
            assert result["status"] == "error"
            assert "[OutOfStock] Out of stock: 0" in (result.get("error") or "")  # wrapped run error
            assert "Node 'Halt' failed" in (result.get("error") or "")
            halt_run = _find_node_run(result, "Halt")
            assert halt_run and halt_run["status"] == "error"
            # downstream must NOT execute: it is recorded as skipped (no active input)
            never_run = _find_node_run(result, "Never")
            assert never_run is not None
            assert never_run["status"] != "success"
            assert "no active input" in (never_run.get("error") or "")
            # no error-workflow binding -> no reroute field
            assert "error_workflow_execution_id" not in result

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids))


# ---------------------------------------------------------------------------
# 3) Sort: field + direction + missing-field accounting
# ---------------------------------------------------------------------------
def test_v22_sort_node():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []

    async def _go():
        async with _client() as client:
            items = [
                {"name": "b", "price": 3},
                {"name": "a", "price": 10},
                {"name": "c"},  # missing price
                {"name": "d", "price": 1},
            ]
            graph = {
                "nodes": [
                    _node("t", "manual_trigger", {"payload": {"items": items}}),
                    _node("s", "sort", {"field": "price", "direction": "desc"}, "Sort"),
                ],
                "edges": [{"id": "e1", "source": "t", "target": "s", "sourceHandle": "main", "targetHandle": "main"}],
            }
            wf_id = await _make_workflow(client, f"tmp v22 sort {tag}", graph)
            wf_ids.append(wf_id)

            result = await _run_and_wait(client, wf_id)
            assert result["status"] == "success", result.get("error")
            run = _find_node_run(result, "Sort")
            out = run.get("output") or {}
            out_payload = out.get("payload", out) if isinstance(out, dict) else {}
            got = out_payload.get("items", [])
            assert [i.get("name") for i in got] == ["c", "a", "b", "d"]  # desc: None first, then 10,3,1
            assert out_payload.get("count") == 4
            assert out_payload.get("missing_field") == 1

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids))


# ---------------------------------------------------------------------------
# 4) Limit: first/last + zero
# ---------------------------------------------------------------------------
def test_v22_limit_node():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []

    async def _go():
        async with _client() as client:
            graph = {
                "nodes": [
                    _node("t", "manual_trigger", {"payload": {"items": [1, 2, 3, 4, 5]}}),
                    _node("l1", "limit", {"max_items": 2, "keep": "first"}, "First2"),
                    _node("l2", "limit", {"max_items": 2, "keep": "last"}, "Last2"),
                ],
                "edges": [
                    {"id": "e1", "source": "t", "target": "l1", "sourceHandle": "main", "targetHandle": "main"},
                    {"id": "e2", "source": "t", "target": "l2", "sourceHandle": "main", "targetHandle": "main"},
                ],
            }
            wf_id = await _make_workflow(client, f"tmp v22 limit {tag}", graph)
            wf_ids.append(wf_id)

            result = await _run_and_wait(client, wf_id)
            assert result["status"] == "success", result.get("error")
            first2 = _find_node_run(result, "First2")
            out = first2.get("output") or {}
            out_payload = out.get("payload", out) if isinstance(out, dict) else {}
            assert out_payload.get("items") == [1, 2]
            assert out_payload.get("kept") == 2 and out_payload.get("total") == 5
            last2 = _find_node_run(result, "Last2")
            out2 = last2.get("output") or {}
            out2_payload = out2.get("payload", out2) if isinstance(out2, dict) else {}
            assert out2_payload.get("items") == [4, 5]

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids))


# ---------------------------------------------------------------------------
# 5) Remove Duplicates: by field and whole-item
# ---------------------------------------------------------------------------
def test_v22_remove_duplicates_node():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []

    async def _go():
        async with _client() as client:
            items = [
                {"email": "a@x.io", "v": 1},
                {"email": "a@x.io", "v": 2},
                {"email": "b@x.io", "v": 3},
                {"email": "b@x.io", "v": 4},
            ]
            graph = {
                "nodes": [
                    _node("t", "manual_trigger", {"payload": {"items": items}}),
                    _node("d", "remove_duplicates", {"field": "email"}, "Dedupe"),
                ],
                "edges": [{"id": "e1", "source": "t", "target": "d", "sourceHandle": "main", "targetHandle": "main"}],
            }
            wf_id = await _make_workflow(client, f"tmp v22 dedupe {tag}", graph)
            wf_ids.append(wf_id)

            result = await _run_and_wait(client, wf_id)
            assert result["status"] == "success", result.get("error")
            run = _find_node_run(result, "Dedupe")
            out = run.get("output") or {}
            out_payload = out.get("payload", out) if isinstance(out, dict) else {}
            got = out_payload.get("items", [])
            assert [i["v"] for i in got] == [1, 3]  # first occurrences win
            assert out_payload.get("unique") == 2
            assert out_payload.get("duplicates_removed") == 2

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids))


# ---------------------------------------------------------------------------
# 6) Definitions: 26 visible types incl. the five v22 nodes
# ---------------------------------------------------------------------------
def test_v22_node_definitions():
    async def _go():
        async with _client() as client:
            res = await client.get("/node-definitions")
            assert res.status_code == 200, res.text
            defs = {d["type"]: d for d in res.json()["definitions"]}
            assert len(defs) == 37
            for t in ("error_trigger", "stop_and_error", "sort", "limit", "remove_duplicates"):
                assert t in defs, t
            # error trigger is a source-only trigger node
            assert defs["error_trigger"]["inputs"] == []
            assert defs["error_trigger"]["category"] == "triggers"
            # sort exposes the direction select (options list)
            props = defs["sort"]["parameters_schema"].get("properties", {})
            assert set(props.get("direction", {}).get("options", [])) == {"asc", "desc"}

    asyncio.run(_go())
