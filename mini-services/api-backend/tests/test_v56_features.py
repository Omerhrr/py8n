"""V56 feature tests: Workflow Intelligence.

WORKFLOW HEALTH: GET /workflows/{id}/health folds the workflow's finished
runs (default 30d window) into a derived report - runs, success rate,
avg/p95 duration, failures, retries (extra attempts from execution
policies / node retry settings), fallbacks used, most-failing node and
most-expensive node - nothing stored, so it cannot drift.

WORKFLOW DIFF: GET /workflows/{id}/versions/diff?from=&to= compares two
WorkflowVersion snapshots at the node/param/edge level (added/removed/
renamed nodes, per-parameter change lines, resilience-settings changes
like "Retry policy: 0 -> 4", edge changes) plus a potential execution-
time impact estimate derived from the workflow's own run history (honest
"estimate unavailable" with no history).

Runs the FastAPI app in-process via httpx ASGITransport (same harness as v4-v55).
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


def _node(nid: str, ntype: str, params: dict | None = None, name: str | None = None, settings: dict | None = None) -> dict:
    n = {"id": nid, "type": ntype, "name": name or nid, "position": {"x": 0, "y": 0}, "parameters": params or {}}
    if settings is not None:
        n["settings"] = settings
    return n


def _edge(eid: str, source: str, target: str) -> dict:
    return {"id": eid, "source": source, "target": target, "sourceHandle": "main", "targetHandle": "main"}


async def _mk_user(client: httpx.AsyncClient, tag: str, n: int) -> dict:
    res = await client.post("/auth/register", json={
        "email": f"v56-{tag}-u{n}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v56 u{n} {tag}",
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


# --------------------------------------------------------------------- health


def test_v56_workflow_health_report():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"health-{tag}", 1)
            h = _auth(user["token"])

            # healthy path: manual trigger -> code node
            graph = {
                "nodes": [
                    _node("t1", "manual_trigger"),
                    _node("gen", "code", params={"code": "result = {'n': 41 + 1}"}),
                ],
                "edges": [_edge("e1", "t1", "gen")],
            }
            res = await client.post("/workflows", headers=h, json={"name": "health-ok", "graph": graph})
            assert res.status_code == 201, res.text
            wf_ok = res.json()

            # failing path: a code node that raises (workflow policy retries it
            # twice) + a fallback node that keeps the flow alive
            bad_graph = {
                "nodes": [
                    _node("t1", "manual_trigger"),
                    _node("boom", "code", params={"code": "result = 1 / 0"}),
                    _node("fb", "code", params={"code": "result = 1 / 0"},
                          settings={"fallback_enabled": True, "fallback_value": {"safe": 1}}),
                ],
                "edges": [_edge("e1", "t1", "boom"), _edge("e2", "t1", "fb")],
            }
            res = await client.post("/workflows", headers=h, json={
                "name": "health-bad", "graph": bad_graph,
                "policy": {"retries": 2, "backoff_ms": 1},
            })
            assert res.status_code == 201, res.text
            wf_bad = res.json()

            # fallback-only path: the failing node emits its fallback value, so
            # the RUN succeeds while the node still records an error + fallback
            fb_graph = {
                "nodes": [
                    _node("t1", "manual_trigger"),
                    _node("soft", "code", params={"code": "result = 1 / 0"},
                          settings={"fallback_enabled": True, "fallback_value": {"safe": 1}}),
                ],
                "edges": [_edge("e1", "t1", "soft")],
            }
            res = await client.post("/workflows", headers=h, json={"name": "health-fb", "graph": fb_graph})
            assert res.status_code == 201, res.text
            wf_fb = res.json()

            # seed: 2 successful runs + 2 failing runs (with retries) + 1 fallback run
            await _run_and_wait(client, wf_ok["id"], h)
            ok2 = await _run_and_wait(client, wf_ok["id"], h)
            assert ok2["status"] == "success", ok2
            failed = await _run_and_wait(client, wf_bad["id"], h)
            assert failed["status"] == "error", failed
            failed2 = await _run_and_wait(client, wf_bad["id"], h)
            assert failed2["status"] == "error", failed2
            fb_run = await _run_and_wait(client, wf_fb["id"], h)
            assert fb_run["status"] == "success", fb_run  # fallback kept it alive

            res = await client.get(f"/workflows/{wf_bad['id']}/health", headers=h)
            assert res.status_code == 200, res.text
            rep = res.json()
            assert rep["runs"] == 2
            assert rep["succeeded"] == 0 and rep["failed"] == 2
            assert rep["success_rate"] == 0.0
            assert rep["verdict"] == "unhealthy"
            # workflow policy retries=2 applies to EVERY node: boom and fb each
            # burn 2 extra attempts per run -> 4 extra attempts per run, 8 total
            assert rep["retries"] == 8
            # the fallback node fired once per run -> 2 fallbacks total
            assert rep["fallbacks"] == 2
            assert rep["most_failing_node"] is not None
            assert rep["most_failing_node"]["errors"] == 2  # boom AND fb failed once per run
            assert rep["most_expensive_node"] is not None
            assert rep["most_expensive_node"]["share_pct"] > 0
            assert rep["p95_duration_ms"] is not None
            assert rep["last_error"]

            # the fallback-only workflow: run succeeded, node error + fallback counted
            res = await client.get(f"/workflows/{wf_fb['id']}/health", headers=h)
            rep_fb = res.json()
            assert rep_fb["verdict"] == "healthy" and rep_fb["succeeded"] == 1
            assert rep_fb["fallbacks"] == 1 and rep_fb["retries"] == 0
            assert rep_fb["most_failing_node"]["errors"] == 1

            res = await client.get(f"/workflows/{wf_ok['id']}/health", headers=h)
            rep = res.json()
            assert rep["verdict"] == "healthy"
            assert rep["success_rate"] == 100.0
            assert rep["failed"] == 0 and rep["retries"] == 0 and rep["fallbacks"] == 0
            assert rep["most_failing_node"] is None

            # window validation + owner scoping
            res = await client.get(f"/workflows/{wf_ok['id']}/health?window_days=0", headers=h)
            assert res.status_code == 422
            other = await _mk_user(client, f"health-{tag}", 2)
            res = await client.get(f"/workflows/{wf_ok['id']}/health", headers=_auth(other["token"]))
            assert res.status_code == 404

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


# ---------------------------------------------------------------- diff


def test_v56_workflow_version_diff():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"diff-{tag}", 1)
            h = _auth(user["token"])

            g1 = {
                "nodes": [
                    _node("t1", "manual_trigger"),
                    _node("gen", "code", params={"code": "result = {'v': 1}"}, name="Generator"),
                ],
                "edges": [_edge("e1", "t1", "gen")],
            }
            res = await client.post("/workflows", headers=h, json={"name": "diff-me", "graph": g1})
            assert res.status_code == 201, res.text
            wf = res.json()

            # v2: change params, rename the node, add a node with retry settings
            g2 = {
                "nodes": [
                    _node("t1", "manual_trigger"),
                    _node("gen", "code", params={"code": "result = {'v': 2}"}, name="Producer"),
                    _node("val", "code", params={"code": "result = {'validated': True}"}, name="Validator",
                          settings={"retry_on_fail": True, "max_retries": 4}),
                ],
                "edges": [_edge("e1", "t1", "gen"), _edge("e2", "gen", "val")],
            }
            res = await client.put(f"/workflows/{wf['id']}", headers=h, json={"graph": g2})
            assert res.status_code == 200, res.text

            # v3: remove the validator again (and its edge)
            res = await client.put(f"/workflows/{wf['id']}", headers=h, json={"graph": g1})
            assert res.status_code == 200, res.text

            # default diff = the two most recent versions (2 -> 3): validator removed
            res = await client.get(f"/workflows/{wf['id']}/versions/diff", headers=h)
            assert res.status_code == 200, res.text
            d2 = res.json()
            assert d2["from"]["version"] == 2 and d2["to"]["version"] == 3
            assert [n["node_id"] for n in d2["removed"]] == ["val"]
            assert len(d2["edges_removed"]) == 1

            res = await client.get(f"/workflows/{wf['id']}/versions/diff?from=1&to=2", headers=h)
            assert res.status_code == 200, res.text
            d = res.json()
            assert d["from"]["version"] == 1 and d["to"]["version"] == 2
            assert [n["node_id"] for n in d["added"]] == ["val"]
            assert d["added"][0]["type"] == "code"
            assert not d["removed"]
            assert d["renamed"] and d["renamed"][0]["old"] == "Generator" and d["renamed"][0]["new"] == "Producer"
            changed = {c["node_id"]: c for c in d["changed"]}
            assert "gen" in changed
            gen = changed["gen"]
            assert gen["changes"][0]["param"] == "code"
            assert "{'v': 1}" in str(gen["changes"][0]["old"])
            # gen's only change is its param - no settings line, no fancy summary
            assert gen["summary"] is None
            # the validator is an ADDED node - its retry settings never land in changed
            assert "val" not in changed
            assert d["edges_added"] == ["Producer -> Validator"]
            assert "graphs identical" not in d["summary"]

            # a settings-only change surfaces as "Retry policy: X -> Y"
            g3 = {
                "nodes": [
                    _node("t1", "manual_trigger"),
                    _node("gen", "code", params={"code": "result = {'v': 2}"}, name="Producer",
                          settings={"retry_on_fail": True, "max_retries": 4}),
                ],
                "edges": [_edge("e1", "t1", "gen")],
            }
            res = await client.put(f"/workflows/{wf['id']}", headers=h, json={"graph": g3})
            assert res.status_code == 200, res.text
            res = await client.get(f"/workflows/{wf['id']}/versions/diff?from=3&to=4", headers=h)
            d4 = res.json()
            gen4 = {c["node_id"]: c for c in d4["changed"]}["gen"]
            assert gen4["summary"] == "Retry policy: 2 -> 4"
            assert any(c["param"] == "max_retries" and c["new"] == 4 for c in gen4["changes"])

            # identical snapshots
            res = await client.get(f"/workflows/{wf['id']}/versions/diff?from=1&to=3", headers=h)
            d3 = res.json()
            assert d3["identical"] is True and d3["summary"] == "graphs identical"

            # errors: unknown version 404, single-version workflow 400
            res = await client.get(f"/workflows/{wf['id']}/versions/diff?from=1&to=99", headers=h)
            assert res.status_code == 404
            res = await client.post("/workflows", headers=h, json={"name": "solo", "graph": g1})
            solo = res.json()
            res = await client.get(f"/workflows/{solo['id']}/versions/diff", headers=h)
            assert res.status_code == 400

            # other users' workflows look nonexistent
            other = await _mk_user(client, f"diff-{tag}", 2)
            res = await client.get(f"/workflows/{wf['id']}/versions/diff?from=1&to=2", headers=_auth(other["token"]))
            assert res.status_code == 404

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


def test_v56_diff_impact_estimate():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"impact-{tag}", 1)
            h = _auth(user["token"])

            g1 = {
                "nodes": [
                    _node("t1", "manual_trigger"),
                    _node("slow", "code", params={"code": "result = {'x': sum(range(2000))}"}),
                ],
                "edges": [_edge("e1", "t1", "slow")],
            }
            res = await client.post("/workflows", headers=h, json={"name": "impact-est", "graph": g1})
            assert res.status_code == 201, res.text
            wf = res.json()

            g2 = {
                "nodes": [
                    _node("t1", "manual_trigger"),
                    _node("slow", "code", params={"code": "result = {'x': sum(range(3000))}"}),
                ],
                "edges": [_edge("e1", "t1", "slow")],
            }
            res = await client.put(f"/workflows/{wf['id']}", headers=h, json={"graph": g2})
            assert res.status_code == 200, res.text

            # no run history yet -> the estimate says so honestly
            res = await client.get(f"/workflows/{wf['id']}/versions/diff", headers=h)
            assert res.status_code == 200, res.text
            d = res.json()
            assert d["potential_impact"]["estimate"] is None
            assert "no run history" in d["potential_impact"]["detail"]

            # seed runs so the changed node has timing history
            for _ in range(2):
                run = await _run_and_wait(client, wf["id"], h)
                assert run["status"] == "success"

            res = await client.get(f"/workflows/{wf['id']}/versions/diff?from=1&to=2", headers=h)
            d = res.json()
            imp = d["potential_impact"]
            assert imp["runs_analyzed"] >= 2
            assert imp["node_samples"] >= 2  # the changed node ran in both seeded runs
            assert imp["estimate"] is not None
            assert "analyzed runs" in imp["detail"]
            assert d["changed"][0]["node_id"] == "slow"

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())
