"""V21 feature tests: Respond to Webhook node.

Covers the n8n respond-early pattern: a webhook run with response_mode=
"respond_node" waits until a respond_to_webhook node answers with a custom
status/body, the flow KEEPS running downstream after answering, JSON and
plain-text bodies, first-respond-wins semantics, flows that finish without
responding (404), standalone respond nodes in manual runs (explicit error),
and invalid JSON bodies (node error).

Runs the FastAPI app in-process via httpx ASGITransport (same harness as
v4-v20).
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


def _webhook_node(mode: str = "respond_node") -> dict:
    return {
        "id": "h",
        "type": "webhook_trigger",
        "name": "Hook",
        "position": {"x": 0, "y": 0},
        "parameters": {"response_mode": mode, "allowed_methods": "POST"},
    }


async def _make_active_workflow(client: httpx.AsyncClient, name: str, graph: dict, wf_ids: list[str]) -> str:
    res = await client.post("/workflows", json={"name": name, "graph": graph, "is_active": False})
    assert res.status_code == 201, res.text
    wf = res.json()
    wf_ids.append(wf["id"])
    res = await client.post(f"/workflows/{wf['id']}/activate")
    assert res.status_code == 200, res.text
    return wf["id"]


# ------------------------------------------------------------------ test 1
def test_respond_node_happy_path_json_and_flow_continues():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []

    async def _go():
        async with _client() as client:
            graph = {
                "nodes": [
                    _webhook_node(),
                    {
                        "id": "e",
                        "type": "code",
                        "name": "Enricher",
                        "position": {"x": 220, "y": 0},
                        "parameters": {
                            "code": (
                                "src = input_data.get('body') or {}\n"
                                "result = {'ticket': src.get('ticket'), 'level': (src.get('level') or 'normal')}\n"
                            )
                        },
                    },
                    {
                        "id": "r",
                        "type": "respond_to_webhook",
                        "name": "Answer caller",
                        "position": {"x": 440, "y": 0},
                        "parameters": {
                            "status_code": 202,
                            "body": '{"ticket": "{{ nodes.e.output.result.ticket }}", "level": "{{ nodes.e.output.result.level }}", "accepted": true}',
                            "content_type": "application/json",
                        },
                    },
                    {
                        "id": "d",
                        "type": "set_variable",
                        "name": "After respond",
                        "position": {"x": 660, "y": 0},
                        "parameters": {"assignments": {"done": "{{ nodes.e.output.result.ticket }}"}, "keep_input": False},
                    },
                ],
                "edges": [
                    {"id": "e1", "source": "h", "target": "e", "sourceHandle": "main", "targetHandle": "main"},
                    {"id": "e2", "source": "e", "target": "r", "sourceHandle": "main", "targetHandle": "main"},
                    {"id": "e3", "source": "r", "target": "d", "sourceHandle": "main", "targetHandle": "main"},
                ],
            }
            wf_id = await _make_active_workflow(client, f"tmp v21 respond {tag}", graph, wf_ids)

            # Fire the webhook: custom 202 + resolved JSON body
            res = await client.post(f"/webhooks/{wf_id}", json={"ticket": "T-100", "level": "urgent"})
            assert res.status_code == 202, res.text
            body = res.json()
            assert body == {"ticket": "T-100", "level": "urgent", "accepted": True}, body

            # The flow kept running after the respond: downstream node executed
            res = await client.get(f"/executions?workflow_id={wf_id}&limit=5")
            execs = [e for e in res.json() if e["trigger_type"] == "webhook"]
            assert len(execs) == 1, res.text
            exec_id = execs[0]["id"]
            for _ in range(60):
                res = await client.get(f"/executions/{exec_id}")
                if res.json()["status"] != "running":
                    break
                await asyncio.sleep(0.05)
            detail = res.json()
            assert detail["status"] == "success", detail.get("error")
            runs = {r["node_id"]: r for r in detail["node_runs"]}
            assert runs["r"]["status"] == "success", runs["r"]
            assert runs["d"]["status"] == "success", runs["d"]
            assert runs["d"]["output"]["done"] == "T-100"

            # definitions registry: 21 visible node types now include the respond node
            res = await client.get("/node-definitions")
            types = [d["type"] for d in res.json()["definitions"]]
            assert "respond_to_webhook" in types and len(types) == 26, types  # 21 after v21 + 5 v22 nodes

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids))


# ------------------------------------------------------------------ test 2
def test_respond_node_negative_paths_and_text_mode():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []

    async def _go():
        async with _client() as client:
            # (a) respond_node mode + flow WITHOUT a respond node -> 404
            graph_no_respond = {
                "nodes": [
                    _webhook_node(),
                    {
                        "id": "s",
                        "type": "set_variable",
                        "name": "Map",
                        "position": {"x": 220, "y": 0},
                        "parameters": {"assignments": {"x": "1"}, "keep_input": False},
                    },
                ],
                "edges": [{"id": "e1", "source": "h", "target": "s", "sourceHandle": "main", "targetHandle": "main"}],
            }
            wf_a = await _make_active_workflow(client, f"tmp v21 norespond {tag}", graph_no_respond, wf_ids)
            res = await client.post(f"/webhooks/{wf_a}", json={"ping": 1})
            assert res.status_code == 404, res.text
            assert "without calling" in res.json()["detail"], res.text

            # (b) plain-text respond body via single-expression template
            graph_text = {
                "nodes": [
                    _webhook_node(),
                    {
                        "id": "r",
                        "type": "respond_to_webhook",
                        "name": "Text answer",
                        "position": {"x": 220, "y": 0},
                        "parameters": {
                            "status_code": 200,
                            "body": "{{ nodes.h.output.body.msg }}",
                            "content_type": "text/plain",
                        },
                    },
                ],
                "edges": [{"id": "e1", "source": "h", "target": "r", "sourceHandle": "main", "targetHandle": "main"}],
            }
            wf_b = await _make_active_workflow(client, f"tmp v21 text {tag}", graph_text, wf_ids)
            res = await client.post(f"/webhooks/{wf_b}", json={"msg": "hello world"})
            assert res.status_code == 200, res.text
            assert res.headers["content-type"].startswith("text/plain"), res.headers
            assert res.text == "hello world", res.text

            # (c) two respond nodes: FIRST wins, second still executes (no-op)
            graph_two = {
                "nodes": [
                    _webhook_node(),
                    {
                        "id": "r1",
                        "type": "respond_to_webhook",
                        "name": "First",
                        "position": {"x": 220, "y": 0},
                        "parameters": {"status_code": 200, "body": '{"first": true}', "content_type": "application/json"},
                    },
                    {
                        "id": "r2",
                        "type": "respond_to_webhook",
                        "name": "Second",
                        "position": {"x": 440, "y": 0},
                        "parameters": {"status_code": 299, "body": '{"second": true}', "content_type": "application/json"},
                    },
                ],
                "edges": [
                    {"id": "e1", "source": "h", "target": "r1", "sourceHandle": "main", "targetHandle": "main"},
                    {"id": "e2", "source": "r1", "target": "r2", "sourceHandle": "main", "targetHandle": "main"},
                ],
            }
            wf_c = await _make_active_workflow(client, f"tmp v21 two {tag}", graph_two, wf_ids)
            res = await client.post(f"/webhooks/{wf_c}", json={})
            assert res.status_code == 200, res.text
            assert res.json() == {"first": True}, res.text

            # (d) manual run + respond node -> explicit error (no caller)
            graph_manual = {
                "nodes": [
                    {"id": "t", "type": "manual_trigger", "name": "Trigger", "position": {"x": 0, "y": 0}, "parameters": {"payload": {}}},
                    {
                        "id": "r",
                        "type": "respond_to_webhook",
                        "name": "Answer nobody",
                        "position": {"x": 220, "y": 0},
                        "parameters": {"status_code": 200, "body": '{"ok": true}', "content_type": "application/json"},
                    },
                ],
                "edges": [{"id": "e1", "source": "t", "target": "r", "sourceHandle": "main", "targetHandle": "main"}],
            }
            res = await client.post("/workflows", json={"name": f"tmp v21 manual {tag}", "graph": graph_manual, "is_active": False})
            assert res.status_code == 201, res.text
            wf_d = res.json()
            wf_ids.append(wf_d["id"])
            res = await client.post(f"/workflows/{wf_d['id']}/run", json={"payload": {}})
            assert res.status_code in (200, 202), res.text
            exec_id = res.json()["execution_id"]
            for _ in range(60):
                res = await client.get(f"/executions/{exec_id}")
                if res.json()["status"] != "running":
                    break
                await asyncio.sleep(0.05)
            detail = res.json()
            assert detail["status"] == "error", detail
            runs = {r["node_id"]: r for r in detail["node_runs"]}
            assert runs["r"]["status"] == "error", runs["r"]
            assert "no caller" in (runs["r"]["error"] or ""), runs["r"]

            # (e) invalid JSON body with content_type=json -> node error
            graph_bad = {
                "nodes": [
                    _webhook_node(),
                    {
                        "id": "r",
                        "type": "respond_to_webhook",
                        "name": "Bad JSON",
                        "position": {"x": 220, "y": 0},
                        "parameters": {"status_code": 200, "body": "not-json {{ nodes.h.output.body.msg }}", "content_type": "application/json"},
                    },
                ],
                "edges": [{"id": "e1", "source": "h", "target": "r", "sourceHandle": "main", "targetHandle": "main"}],
            }
            wf_e = await _make_active_workflow(client, f"tmp v21 badjson {tag}", graph_bad, wf_ids)
            res = await client.post(f"/webhooks/{wf_e}", json={"msg": "x"})
            # the node errors BEFORE responding -> flow errored, no answer given
            assert res.status_code == 500, res.text
            assert "errored before responding" in res.json()["detail"], res.text
            assert "not valid JSON" in res.json()["detail"], res.text

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids))
