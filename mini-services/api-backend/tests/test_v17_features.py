"""V17 feature tests: pinned node data (n8n-style mock outputs) + test step.

Covers: pinned_data persistence through the graph schema (save → GET round
trip), pin honoring semantics (manual runs + sub-workflows of manual runs +
loop batches honor pins; webhook-triggered production runs execute for real),
the ``pinned`` flag on node_runs, empty-list pins, the single-node test-step
endpoint (real execution with ad-hoc input, pinned preview, error surfacing,
no execution log left behind) and 404 guards.

Runs the FastAPI app in-process via httpx ASGITransport against the dev
SQLite DB (same harness as v4-v16). All assertions scope to workflows created
here (uuid-suffixed names) so dev data never flakes.
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


def _graph(trigger_type: str = "manual_trigger", with_set: bool = True) -> dict:
    """trigger → code (reads input n) → set (maps the code result)."""
    trigger_params = {"payload": {}} if trigger_type == "manual_trigger" else {}
    nodes = [
        {"id": "t", "type": trigger_type, "name": "Trigger", "position": {"x": 0, "y": 0}, "parameters": trigger_params},
        {
            "id": "c",
            "type": "code",
            "name": "Doubler",
            "position": {"x": 220, "y": 0},
            "parameters": {"code": "src = input_data.get('payload') or input_data.get('body') or {}\nresult = {'doubled': src['n'] * 2}\n"},
        },
    ]
    edges = [{"id": "e1", "source": "t", "target": "c", "sourceHandle": "main", "targetHandle": "main"}]
    if with_set:
        nodes.append(
            {
                "id": "s",
                "type": "set_variable",
                "name": "Map",
                "position": {"x": 440, "y": 0},
                "parameters": {"assignments": {"from_code": "{{ nodes.c.output.result.doubled }}"}, "keep_input": False},
            }
        )
        edges.append({"id": "e2", "source": "c", "target": "s", "sourceHandle": "main", "targetHandle": "main"})
    return {"nodes": nodes, "edges": edges}


async def _run_and_wait(client: httpx.AsyncClient, workflow_id: str, payload: dict | None = None) -> dict:
    res = await client.post(f"/workflows/{workflow_id}/run", json={"payload": payload or {}})
    assert res.status_code in (200, 202), res.text
    exec_id = res.json()["execution_id"]
    for _ in range(80):
        res = await client.get(f"/executions/{exec_id}")
        assert res.status_code == 200, res.text
        if res.json()["status"] != "running":
            return res.json()
        await asyncio.sleep(0.05)
    raise AssertionError("execution did not finish in time")


# ------------------------------------------------------------------ test 1
def test_pin_semantics_manual_vs_production_and_test_step():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []

    async def _go():
        async with _client() as client:
            # --- create the workflow (unpinned) ---------------------------
            res = await client.post(
                "/workflows",
                json={"name": f"tmp v17 pin {tag}", "graph": _graph(), "is_active": False},
            )
            assert res.status_code == 201, res.text
            wf = res.json()
            wf_ids.append(wf["id"])

            # baseline manual run: real execution
            detail = await _run_and_wait(client, wf["id"], {"n": 7})
            assert detail["status"] == "success", detail.get("error")
            runs = {r["node_id"]: r for r in detail["node_runs"]}
            assert runs["c"]["output"]["result"]["doubled"] == 14, runs["c"]
            assert "pinned" not in runs["c"]
            assert runs["s"]["output"]["from_code"] == 14

            # --- pin the code node; persists through save → GET ----------
            res = await client.get(f"/workflows/{wf['id']}")
            graph = res.json()["graph"]
            for node in graph["nodes"]:
                if node["id"] == "c":
                    node["pinned_data"] = {"result": {"doubled": 100}}
            res = await client.put(f"/workflows/{wf['id']}", json={"graph": graph})
            assert res.status_code == 200, res.text
            saved = next(n for n in res.json()["graph"]["nodes"] if n["id"] == "c")
            assert saved["pinned_data"] == {"result": {"doubled": 100}}, saved

            # manual run now honors the pin: code node never executes
            detail = await _run_and_wait(client, wf["id"], {"n": 7})
            assert detail["status"] == "success", detail.get("error")
            runs = {r["node_id"]: r for r in detail["node_runs"]}
            assert runs["c"]["output"] == {"result": {"doubled": 100}}, runs["c"]
            assert runs["c"].get("pinned") is True
            assert runs["c"]["duration_ms"] == 0
            assert runs["s"]["output"]["from_code"] == 100  # downstream sees the pin

            # production path: webhook trigger + direct executor dispatch
            # → pins NOT honored, the node really executes
            res = await client.post(
                "/workflows",
                json={"name": f"tmp v17 prod {tag}", "graph": _graph("webhook_trigger"), "is_active": False},
            )
            assert res.status_code == 201, res.text
            wf_prod = res.json()
            wf_ids.append(wf_prod["id"])
            res = await client.get(f"/workflows/{wf_prod['id']}")
            prod_graph = res.json()["graph"]
            for node in prod_graph["nodes"]:
                if node["id"] == "c":
                    node["pinned_data"] = {"result": {"doubled": 100}}
            res = await client.put(f"/workflows/{wf_prod['id']}", json={"graph": prod_graph})
            assert res.status_code == 200, res.text

            from app.services.executor import execute_workflow

            result = await execute_workflow(
                wf_prod["id"],
                trigger_type="webhook",
                trigger_payload={"method": "POST", "headers": {}, "query": {}, "body": {"n": 7}},
            )
            assert result["status"] == "success", result.get("error")
            runs = {r["node_id"]: r for r in result["node_runs"]}
            assert runs["c"]["output"]["result"]["doubled"] == 14, runs["c"]  # real execution
            assert "pinned" not in runs["c"]
            assert runs["s"]["output"]["from_code"] == 14

            # --- test-step endpoint ---------------------------------------
            # executions list before → count must not grow (test step logs nothing)
            res = await client.get(f"/executions?workflow_id={wf['id']}&limit=50")
            execs_before = len(res.json())

            # pinned node → pinned preview, no execution
            res = await client.post(f"/workflows/{wf['id']}/nodes/c/test", json={"items": {"n": 5}})
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["ok"] is True and body["pinned_used"] is True, body
            assert body["output"] == {"result": {"doubled": 100}}

            # isolated node missing upstream context → clean inline error
            res = await client.post(f"/workflows/{wf['id']}/nodes/s/test", json={})
            assert res.status_code == 200, res.text
            body = res.json()
            # set node references nodes.c.output - absent in isolation
            assert body["ok"] is False and "Unresolved variable" in (body["error"] or ""), body

            # 404 guards
            res = await client.post(f"/workflows/{wf['id']}/nodes/ghost/test", json={})
            assert res.status_code == 404, res.text
            res = await client.post(f"/workflows/does-not-exist/nodes/c/test", json={})
            assert res.status_code == 404, res.text

            # no execution log was created by any test-step call
            res = await client.get(f"/executions?workflow_id={wf['id']}&limit=50")
            assert len(res.json()) == execs_before, "test step must not log executions"

            # --- unpin → real test-step execution + real manual run -------
            res = await client.get(f"/workflows/{wf['id']}")
            graph = res.json()["graph"]
            for node in graph["nodes"]:
                if node["id"] == "c":
                    node["pinned_data"] = None
            res = await client.put(f"/workflows/{wf['id']}", json={"graph": graph})
            assert res.status_code == 200, res.text
            saved = next(n for n in res.json()["graph"]["nodes"] if n["id"] == "c")
            assert saved["pinned_data"] is None, saved

            # test step now REALLY executes with the ad-hoc input
            # (items = direct node input - same shape as a trigger's output)
            res = await client.post(f"/workflows/{wf['id']}/nodes/c/test", json={"items": {"payload": {"n": 21}}})
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["ok"] is True and body["pinned_used"] is False, body
            assert body["output"]["result"]["doubled"] == 42, body
            assert body["duration_ms"] >= 0

            detail = await _run_and_wait(client, wf["id"], {"n": 3})
            runs = {r["node_id"]: r for r in detail["node_runs"]}
            assert runs["c"]["output"]["result"]["doubled"] == 6
            assert "pinned" not in runs["c"]

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids))


# ------------------------------------------------------------------ test 2
def test_empty_pin_and_subworkflow_pin_inheritance():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []

    async def _go():
        async with _client() as client:
            # --- empty-list pin: output [], downstream still runs ----------
            graph = _graph(with_set=False)  # no downstream template to resolve
            for node in graph["nodes"]:
                if node["id"] == "c":
                    node["pinned_data"] = []
            res = await client.post(
                "/workflows",
                json={"name": f"tmp v17 empty {tag}", "graph": graph, "is_active": False},
            )
            assert res.status_code == 201, res.text
            wf_empty = res.json()
            wf_ids.append(wf_empty["id"])

            detail = await _run_and_wait(client, wf_empty["id"], {"n": 9})
            assert detail["status"] == "success", detail.get("error")
            runs = {r["node_id"]: r for r in detail["node_runs"]}
            assert runs["c"]["output"] == [] and runs["c"].get("pinned") is True, runs["c"]
            assert runs["c"]["duration_ms"] == 0

            # --- sub-workflow inherits the manual pin decision -------------
            child_graph = _graph(with_set=False)  # last success = the pinned node
            for node in child_graph["nodes"]:
                if node["id"] == "c":
                    node["pinned_data"] = {"result": {"doubled": 999}}
            res = await client.post(
                "/workflows",
                json={"name": f"tmp v17 child {tag}", "graph": child_graph, "is_active": False},
            )
            assert res.status_code == 201, res.text
            child = res.json()
            wf_ids.append(child["id"])

            parent_graph = {
                "nodes": [
                    {"id": "t", "type": "manual_trigger", "name": "Trigger", "position": {"x": 0, "y": 0}, "parameters": {"payload": {"n": 4}}},
                    {
                        "id": "sub",
                        "type": "execute_workflow",
                        "name": "Call child",
                        "position": {"x": 220, "y": 0},
                        "parameters": {"workflow_id": child["id"], "payload": {"n": 4}, "wait_for_completion": True},
                    },
                ],
                "edges": [{"id": "e1", "source": "t", "target": "sub", "sourceHandle": "main", "targetHandle": "main"}],
            }
            res = await client.post(
                "/workflows",
                json={"name": f"tmp v17 parent {tag}", "graph": parent_graph, "is_active": False},
            )
            assert res.status_code == 201, res.text
            parent = res.json()
            wf_ids.append(parent["id"])

            detail = await _run_and_wait(client, parent["id"], {})
            assert detail["status"] == "success", detail.get("error")
            runs = {r["node_id"]: r for r in detail["node_runs"]}
            # the sub-run's pinned code node surfaced through the parent's sub node
            assert runs["sub"]["output"]["output"] == {"result": {"doubled": 999}}, runs["sub"]

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids))
