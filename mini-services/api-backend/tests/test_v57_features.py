"""V57 feature tests: Automation Operations Center.

OPS OVERVIEW: GET /ops/overview composes the whole-environment rollup -
SYSTEM verdict, workflows (total/active/running now/runs 24h/failed 24h/
failures 7d + failing list), datasets (v53 health tiers), reports
(scheduled + delivery outcomes), agents (workflows carrying agent/llm
nodes + their 7d record) and the 72h incidents with execution ids.

INCIDENT DRILLDOWN: GET /ops/incidents/{execution_id} walks the chain
workflow -> execution -> failed node (with the input it received) ->
error -> previous successful run comparison -> related datasets (with
live health) -> v55 impact - derived on the spot, owner-scoped, 404 for
unknown executions and other users' executions.

Runs the FastAPI app in-process via httpx ASGITransport (same harness as v4-v56).
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


def _node(nid: str, ntype: str, params: dict | None = None, name: str | None = None) -> dict:
    return {"id": nid, "type": ntype, "name": name or nid, "position": {"x": 0, "y": 0}, "parameters": params or {}}


def _edge(eid: str, source: str, target: str) -> dict:
    return {"id": eid, "source": source, "target": target, "sourceHandle": "main", "targetHandle": "main"}


async def _mk_user(client: httpx.AsyncClient, tag: str, n: int) -> dict:
    res = await client.post("/auth/register", json={
        "email": f"v57-{tag}-u{n}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v57 u{n} {tag}",
    })
    assert res.status_code == 201, res.text
    body = res.json()
    return {"token": body["token"], "id": body["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _run_and_wait(client: httpx.AsyncClient, wf_id: str, headers: dict) -> dict:
    res = await client.post(f"/workflows/{wf_id}/run", headers=headers, json={})
    assert res.status_code in (200, 202), res.text
    exec_id = res.json()["execution_id"]
    for _ in range(200):
        res = await client.get(f"/executions/{exec_id}", headers=headers)
        assert res.status_code == 200, res.text
        if res.json()["status"] not in ("running", "queued"):
            return res.json()
        await asyncio.sleep(0.05)
    raise AssertionError("execution did not finish in time")


def test_v57_ops_overview():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"ops-{tag}", 1)
            h = _auth(user["token"])

            # a dataset + a healthy workflow + a failing workflow + an agent workflow
            res = await client.post("/datasets", headers=h, json={"name": f"ops-ds-{tag}", "rows": [{"a": 1}, {"a": 2}]})
            assert res.status_code == 201, res.text

            ok_graph = {"nodes": [_node("t1", "manual_trigger"),
                                  _node("gen", "code", {"code": "result = {'ok': 1}"})],
                        "edges": [_edge("e1", "t1", "gen")]}
            res = await client.post("/workflows", headers=h, json={"name": "ops-ok", "graph": ok_graph})
            wf_ok = res.json()
            bad_graph = {"nodes": [_node("t1", "manual_trigger"),
                                   _node("boom", "code", {"code": "result = 1 / 0"})],
                         "edges": [_edge("e1", "t1", "boom")]}
            res = await client.post("/workflows", headers=h, json={"name": "ops-bad", "graph": bad_graph})
            wf_bad = res.json()
            agent_graph = {"nodes": [_node("t1", "manual_trigger"),
                                     _node("ai", "llm_chat", {"user_prompt": "hi {{ input }}"}),
                                     _node("boom", "code", {"code": "result = 1 / 0"})],
                           "edges": [_edge("e1", "t1", "ai"), _edge("e2", "ai", "boom")]}
            res = await client.post("/workflows", headers=h, json={"name": "ops-agent", "graph": agent_graph})
            wf_agent = res.json()

            await _run_and_wait(client, wf_ok["id"], h)
            failed = await _run_and_wait(client, wf_bad["id"], h)
            assert failed["status"] == "error"
            agent_failed = await _run_and_wait(client, wf_agent["id"], h)
            assert agent_failed["status"] == "error"

            res = await client.get("/ops/overview", headers=h)
            assert res.status_code == 200, res.text
            ops = res.json()
            assert ops["verdict"] in ("healthy", "degraded", "unhealthy")
            assert ops["verdict"] == "degraded"  # failures exist, not catastrophic
            assert ops["workflows"]["total"] == 3
            assert ops["workflows"]["failed_24h"] >= 2
            assert ops["workflows"]["runs_24h"] >= 3
            assert ops["datasets"]["total"] >= 1
            assert ops["agents"]["agent_workflows"] == 1
            assert ops["agents"]["errors_7d"] >= 1
            assert ops["reports"]["scheduled"] == 0
            # incidents carry execution ids for drilldown
            assert ops["incidents"], "failed runs must surface as incidents"
            failed_incidents = [e for e in ops["incidents"] if e["type"] == "workflow.failed"]
            assert failed_incidents
            assert all("execution_id" in e["meta"] for e in failed_incidents)

            # a second user sees none of this estate
            other = await _mk_user(client, f"ops-{tag}", 2)
            res = await client.get("/ops/overview", headers=_auth(other["token"]))
            ops2 = res.json()
            assert ops2["workflows"]["total"] == 0
            assert ops2["incidents"] == []

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


def test_v57_incident_drilldown():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"drill-{tag}", 1)
            h = _auth(user["token"])

            # upstream dataset the workflow writes to -> related + impact
            res = await client.post("/datasets", headers=h, json={"name": f"drill-ds-{tag}", "rows": [{"a": 1}]})
            ds = res.json()

            # workflow: trigger -> code -> dataset_write; the middle node fails,
            # then a PREVIOUS successful run exists for comparison
            graph = {"nodes": [
                _node("t1", "manual_trigger"),
                _node("calc", "code", {"code": "result = {'x': 1}"}),
                _node("save", "dataset_write", {"dataset": ds["name"], "rows": "{{ nodes.calc.output }}"}),
            ], "edges": [_edge("e1", "t1", "calc"), _edge("e2", "calc", "save")]}
            res = await client.post("/workflows", headers=h, json={"name": "drill-wf", "graph": graph})
            assert res.status_code == 201, res.text
            wf = res.json()

            good = await _run_and_wait(client, wf["id"], h)
            assert good["status"] == "success", good

            # break the middle node and run again
            graph_bad = {"nodes": [
                _node("t1", "manual_trigger"),
                _node("calc", "code", {"code": "result = 1 / 0"}),
                _node("save", "dataset_write", {"dataset": ds["name"], "rows": "{{ nodes.calc.output }}"}),
            ], "edges": [_edge("e1", "t1", "calc"), _edge("e2", "calc", "save")]}
            res = await client.put(f"/workflows/{wf['id']}", headers=h, json={"graph": graph_bad})
            assert res.status_code == 200, res.text
            bad = await _run_and_wait(client, wf["id"], h)
            assert bad["status"] == "error", bad

            res = await client.get(f"/ops/incidents/{bad['id']}", headers=h)
            assert res.status_code == 200, res.text
            chain = res.json()

            # the full chain is present
            steps = [s["step"] for s in chain["chain"]]
            assert steps == ["workflow", "execution", "node", "input", "error",
                             "previous_success", "related_datasets", "impact"]
            assert chain["execution"]["id"] == bad["id"]
            assert chain["workflow"]["id"] == wf["id"]
            assert chain["failed_node"] is not None
            assert chain["failed_node"]["node_id"] == "calc"
            assert "ZeroDivision" in str(chain["failed_node"]["error"])
            # comparison against the previous success
            comp = chain["comparison_with_previous_success"]
            assert comp["previous_execution_id"] == good["id"]
            assert comp["node"]["present_in_previous"] is True
            assert comp["node"]["previous_status"] == "success"
            # the dataset the workflow writes shows up, with live health
            related_names = [d["name"] for d in chain["related_datasets"]]
            assert ds["name"] in related_names
            # impact was computed for the related dataset
            assert chain["impact"], "impact must ride on the related datasets"
            assert chain["severity"] in ("low", "medium", "high", "critical", "info")

            # errors: unknown execution + other user's execution
            res = await client.get("/ops/incidents/nonexistent-exec", headers=h)
            assert res.status_code == 404
            other = await _mk_user(client, f"drill-{tag}", 2)
            res = await client.get(f"/ops/incidents/{bad['id']}", headers=_auth(other["token"]))
            assert res.status_code == 404  # other users' estates are invisible

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())
