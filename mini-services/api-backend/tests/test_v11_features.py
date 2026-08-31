"""V11 feature tests: aggregated execution insights.

Covers: GET /insights - summary counts + success-rate semantics (only
finished runs count), zero-filled per-day timeline, per-workflow scoping
(workflow_id), top-workflow leaderboard, node-type aggregation from
persisted node_runs, trigger breakdown, and window validation.

Runs the FastAPI app in-process via httpx ASGITransport against the dev
SQLite DB (same harness as v4-v10). Assertions are scoped to workflows
created here, so pre-existing dev data never flakes a run.
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
    if not workflow_ids:
        return
    async with _client() as client:
        for wid in workflow_ids:
            try:
                await client.delete(f"/workflows/{wid}")
            except Exception:
                pass
    await _drain_background()


def _code_graph(code: str) -> dict:
    return {
        "nodes": [
            {"id": "t1", "type": "manual_trigger", "name": "Manual"},
            {
                "id": "c1",
                "type": "code",
                "name": "Code",
                "parameters": {"language": "python", "code": code},
            },
        ],
        "edges": [{"id": "e1", "source": "t1", "target": "c1", "sourceHandle": "main", "targetHandle": "main"}],
    }


def _wait_graph() -> dict:
    return {
        "nodes": [
            {"id": "t1", "type": "manual_trigger", "name": "Manual"},
            {"id": "w1", "type": "wait_for_resume", "name": "Wait", "parameters": {}},
        ],
        "edges": [{"id": "e1", "source": "t1", "target": "w1", "sourceHandle": "main", "targetHandle": "main"}],
    }


async def _create(client: httpx.AsyncClient, name: str, graph: dict) -> str:
    res = await client.post("/workflows", json={"name": name, "graph": graph})
    assert res.status_code == 201, res.text
    return res.json()["id"]


async def _run(client: httpx.AsyncClient, wf_id: str, payload: dict | None = None) -> str:
    res = await client.post(f"/workflows/{wf_id}/run", json={"payload": payload or {"hello": "v11"}})
    assert res.status_code in (200, 202), res.text
    return res.json()["execution_id"]


def test_insights_summary_rate_timeline_and_nodes():
    """Success + error + waiting runs aggregate into exact scoped stats."""
    tag = uuid.uuid4().hex[:8]
    created: list[str] = []

    async def _go():
        async with _client() as client:
            ok_wf = await _create(client, f"v11-ok-{tag}", _code_graph("out = {'ok': input_data['payload']['hello']}"))
            created.append(ok_wf)
            bad_wf = await _create(client, f"v11-bad-{tag}", _code_graph("raise RuntimeError('boom-v11')"))
            created.append(bad_wf)
            wait_wf = await _create(client, f"v11-wait-{tag}", _wait_graph())
            created.append(wait_wf)

            # 2 successes, 1 error, 1 waiting
            await _run(client, ok_wf)
            await _run(client, ok_wf)
            await _run(client, bad_wf)
            await _run(client, wait_wf)
            await _drain_background()

            res = await client.get(f"/insights?days=3&workflow_id={ok_wf}")
            assert res.status_code == 200, res.text
            body = res.json()

            # window shape: calendar-aligned, one bucket per day incl. today
            assert body["window"]["days"] == 3
            assert len(body["timeline"]) == 3
            assert body["timeline"][-1]["date"] >= body["timeline"][0]["date"]
            today = body["timeline"][-1]
            assert today["total"] == 2 and today["success"] == 2

            s = body["summary"]
            assert s["total"] == 2 and s["success"] == 2
            assert s["success_rate"] == 100.0
            assert s["avg_duration_ms"] >= 0
            assert s["node_runs_total"] >= 4  # trigger+code per run

            # node stats aggregate the persisted runs
            types = {n["node_type"]: n for n in body["node_stats"]}
            assert types["code"]["runs"] == 2 and types["code"]["errors"] == 0
            assert types["manual_trigger"]["runs"] == 2

            # leaderboard: the only workflow in scope is itself, 100%
            assert len(body["top_workflows"]) == 1
            assert body["top_workflows"][0]["workflow_id"] == ok_wf
            assert body["top_workflows"][0]["success_rate"] == 100.0

            # trigger breakdown counts only this workflow's runs
            assert body["trigger_breakdown"].get("manual") == 2

            # ---- error-scoped rate: finished-only semantics
            res_bad = await client.get(f"/insights?days=3&workflow_id={bad_wf}")
            bad = res_bad.json()["summary"]
            assert bad["total"] == 1 and bad["error"] == 1 and bad["success_rate"] == 0.0

            # ---- waiting runs don't dilute success_rate (not finished)
            res_wait = await client.get(f"/insights?days=3&workflow_id={wait_wf}")
            wait = res_wait.json()["summary"]
            assert wait["total"] == 1 and wait["waiting"] == 1
            assert wait["success_rate"] == 0.0 and wait["success"] == 0

            # node run for the wait node carries the waiting status
            wait_types = {n["node_type"]: n for n in res_wait.json()["node_stats"]}
            assert wait_types["wait_for_resume"]["runs"] == 1
        await _drain_background()

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(created))


def test_insights_timeline_zero_fill_and_window_validation():
    tag = uuid.uuid4().hex[:8]
    created: list[str] = []

    async def _go():
        async with _client() as client:
            wf_id = await _create(client, f"v11-fill-{tag}", _code_graph("out = {'n': 1}"))
            created.append(wf_id)
            await _run(client, wf_id)
            await _drain_background()

            res = await client.get(f"/insights?days=7&workflow_id={wf_id}")
            body = res.json()
            assert len(body["timeline"]) == 7
            dates = [b["date"] for b in body["timeline"]]
            assert dates == sorted(dates), "timeline buckets must ascend"
            # all but the last bucket are zero-filled for a fresh workflow
            for bucket in body["timeline"][:-1]:
                assert bucket["total"] == 0
            assert body["timeline"][-1]["total"] == 1

            # global shape over the whole platform (dev DB has history)
            glob = (await client.get("/insights?days=14")).json()
            assert glob["summary"]["total"] >= 1
            assert len(glob["timeline"]) == 14
            assert glob["window"]["since"] < glob["window"]["until"]

            # window validation: 0 and 91 are out of bounds
            assert (await client.get("/insights?days=0")).status_code == 422
            assert (await client.get("/insights?days=91")).status_code == 422

            # unknown workflow scope → honest zeros, not 404
            empty = (await client.get("/insights?days=5&workflow_id=nope")).json()
            assert empty["summary"]["total"] == 0
            assert empty["node_stats"] == [] and empty["top_workflows"] == []
        await _drain_background()

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(created))
