"""V58 feature tests: AI Operations.

INVESTIGATION: POST /ops/ai/investigate walks the roadmap's 7-step
checklist deterministically (no LLM in the loop): workflow identified,
failed execution identified, failed node identified, error inspected,
previous successful run compared, dataset health checked, recent graph
changes checked - then classifies the cause from the error text, writes
a recommendation, derives the affected surface from the v55 impact
engine and (for throttling/timeout causes) builds a STRUCTURED policy
patch proposal. The LLM narration layer is fail-soft: an unreachable
sandbox bridge returns narration=None with a note.

APPLY PROPOSAL: POST /ops/ai/apply-proposal is the "user executes"
half - it validates the patch with the same rules as the settings
editor, applies it to the workflow policy and lands as a new version.

Runs the FastAPI app in-process via httpx ASGITransport (same harness as v4-v57).
"""

from __future__ import annotations

import asyncio
import uuid

import httpx

from app.main import app
from app.services import executor as executor_mod
from app.services.aiops import classify_cause

API = "http://testserver/api/v1"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _drain_background() -> None:
    tasks = [t for t in executor_mod._background_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _node(nid: str, ntype: str, params: dict | None = None) -> dict:
    return {"id": nid, "type": ntype, "name": nid, "position": {"x": 0, "y": 0}, "parameters": params or {}}


def _edge(eid: str, source: str, target: str) -> dict:
    return {"id": eid, "source": source, "target": target, "sourceHandle": "main", "targetHandle": "main"}


async def _mk_user(client: httpx.AsyncClient, tag: str, n: int) -> dict:
    res = await client.post("/auth/register", json={
        "email": f"v58-{tag}-u{n}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v58 u{n} {tag}",
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


# ---------------------------------------------------------------- classifier


def test_v58_cause_classifier():
    """The rule-based classifier covers the roadmap's failure signatures."""
    cases = [
        ("HTTP 429 Too Many Requests - throttled", "rate_limit"),
        ("upstream said: rate limit exceeded", "rate_limit"),
        ("Request timed out after 30s", "timeout"),
        ("HTTP 401 Unauthorized: invalid api key", "auth"),
        ("Connection refused by upstream host", "connection"),
        ("data contract violated: column 'revenue' is not castable", "contract"),
        ("NodeExecutionError: Code error: ZeroDivisionError: division by zero", "code"),
        ("something completely novel happened", "unknown"),
    ]
    for text, kind in cases:
        c = classify_cause(text)
        assert c["kind"] == kind, (text, c)
        assert c["evidence"]
    # confidence is high for known signatures, low for unknown
    assert classify_cause("HTTP 429")["confidence"] == "high"
    assert classify_cause("mystery")["confidence"] == "low"


def test_v58_investigation():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"ai-{tag}", 1)
            h = _auth(user["token"])

            # dataset + workflow: succeed once, then break the node via a graph edit
            res = await client.post("/datasets", headers=h, json={"name": f"ai-ds-{tag}", "rows": [{"a": 1}]})
            ds = res.json()

            graph = {"nodes": [
                _node("t1", "manual_trigger"),
                _node("calc", "code", {"code": "result = {'x': 1}"}),
                _node("save", "dataset_write", {"dataset": ds["name"], "rows": "{{ nodes.calc.output }}"}),
            ], "edges": [_edge("e1", "t1", "calc"), _edge("e2", "calc", "save")]}
            res = await client.post("/workflows", headers=h, json={"name": "ai-wf", "graph": graph})
            wf = res.json()
            good = await _run_and_wait(client, wf["id"], h)
            assert good["status"] == "success"

            graph["nodes"][1]["parameters"]["code"] = "result = 1 / 0"
            res = await client.put(f"/workflows/{wf['id']}", headers=h, json={"graph": graph})
            assert res.status_code == 200, res.text
            bad = await _run_and_wait(client, wf["id"], h)
            assert bad["status"] == "error"

            # --- deterministic investigation (no narration) -----------------
            res = await client.post("/ops/ai/investigate", headers=h, json={
                "execution_id": bad["id"], "narrate": False,
            })
            assert res.status_code == 200, res.text
            f = res.json()

            # the 7-step checklist, in order
            steps = [c["step"] for c in f["checklist"]]
            assert steps == [
                "workflow_identified", "failed_execution_identified", "failed_node_identified",
                "error_inspected", "previous_run_compared", "dataset_health_checked",
                "recent_changes_checked",
            ]
            by_step = {c["step"]: c for c in f["checklist"]}
            assert by_step["workflow_identified"]["ok"] is True
            assert by_step["failed_node_identified"]["ok"] is True
            assert "calc" in by_step["failed_node_identified"]["detail"]
            assert by_step["previous_run_compared"]["ok"] is True
            assert good["id"][:8] in by_step["previous_run_compared"]["detail"]
            assert by_step["dataset_health_checked"]["ok"] is True
            assert ds["name"] in by_step["dataset_health_checked"]["detail"]
            # the graph changed since the last success (we broke the node) - caught
            assert by_step["recent_changes_checked"]["detail"].startswith("graph changed")

            # cause: code error (ZeroDivision), honest, no invented proposal
            assert f["cause"]["kind"] == "code"
            assert "ZeroDivision" in f["cause"]["evidence"]
            assert "code" in f["recommendation"].lower() or "node" in f["recommendation"].lower()
            assert f["proposed_action"] is None
            # the graph-change hint rode along
            assert any("graph changed" in h for h in f["hints"])
            # affected surface rides from the impact engine
            assert ds["name"] in f["affected"]["datasets"]
            assert f["narration"] is None and f["narration_note"] is None
            assert "executes" in f["disclaimer"].lower()

            # --- fail-soft narration: bridge unreachable in the sandbox -----
            res = await client.post("/ops/ai/investigate", headers=h, json={
                "execution_id": bad["id"], "narrate": True,
            })
            f2 = res.json()
            assert f2["narration"] is None
            assert f2["narration_note"] and "bridge" in f2["narration_note"].lower()

            # --- errors ------------------------------------------------------
            res = await client.post("/ops/ai/investigate", headers=h, json={"execution_id": "nope"})
            assert res.status_code == 404
            other = await _mk_user(client, f"ai-{tag}", 2)
            res = await client.post("/ops/ai/investigate", headers=_auth(other["token"]), json={
                "execution_id": bad["id"],
            })
            assert res.status_code == 404

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


def test_v58_apply_proposal():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"apply-{tag}", 1)
            h = _auth(user["token"])

            graph = {"nodes": [_node("t1", "manual_trigger"),
                              _node("gen", "code", {"code": "result = 1"})],
                     "edges": [_edge("e1", "t1", "gen")]}
            res = await client.post("/workflows", headers=h, json={"name": "apply-wf", "graph": graph})
            wf = res.json()
            base_versions = len((await _versions(client, wf["id"], h))["versions"])

            # apply a throttling-style proposal
            res = await client.post("/ops/ai/apply-proposal", headers=h, json={
                "workflow_id": wf["id"],
                "patch": {"retries": 4, "backoff_ms": 8000, "backoff_multiplier": 2.0},
            })
            assert res.status_code == 200, res.text
            out = res.json()
            assert out["policy"]["retries"] == 4
            assert out["policy"]["backoff_ms"] == 8000
            assert out["version"]  # a new version was snapshotted
            versions = await _versions(client, wf["id"], h)
            assert len(versions["versions"]) == base_versions + 1

            # the workflow now carries the policy
            res = await client.get(f"/workflows/{wf['id']}", headers=h)
            assert res.json()["policy"]["retries"] == 4

            # out-of-range + unknown keys are rejected (same rules as settings)
            res = await client.post("/ops/ai/apply-proposal", headers=h, json={
                "workflow_id": wf["id"], "patch": {"retries": 99},
            })
            assert res.status_code == 400
            res = await client.post("/ops/ai/apply-proposal", headers=h, json={
                "workflow_id": wf["id"], "patch": {"hack": True},
            })
            assert res.status_code == 400
            res = await client.post("/ops/ai/apply-proposal", headers=h, json={
                "workflow_id": wf["id"], "patch": {},
            })
            assert res.status_code == 400

            # unknown workflow + other users' workflows
            res = await client.post("/ops/ai/apply-proposal", headers=h, json={
                "workflow_id": "nope", "patch": {"retries": 2},
            })
            assert res.status_code == 404
            other = await _mk_user(client, f"apply-{tag}", 2)
            res = await client.post("/ops/ai/apply-proposal", headers=_auth(other["token"]), json={
                "workflow_id": wf["id"], "patch": {"retries": 2},
            })
            assert res.status_code == 404

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


async def _versions(client: httpx.AsyncClient, wf_id: str, headers: dict) -> dict:
    res = await client.get(f"/workflows/{wf_id}/versions", headers=headers)
    assert res.status_code == 200, res.text
    return res.json()
