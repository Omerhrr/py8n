"""V4 feature tests: execution observability API (status filter, workflow_name, rerun, delete).

Runs the FastAPI app in-process via httpx ASGITransport against the dev SQLite DB.
Each test wraps ALL of its requests in a single asyncio.run loop so the inline
dispatcher's background execution tasks survive between requests; helpers drain
pending tasks before the loop closes.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import httpx

from app.main import app
from app.services import executor as executor_mod

API = "http://testserver/api/v1"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _wait_execution(client: httpx.AsyncClient, exec_id: str, timeout: float = 15.0) -> dict:
    """Poll until the execution leaves the running state."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        res = await client.get(f"/executions/{exec_id}")
        if res.status_code == 200:
            body = res.json()
            if body["status"] != "running":
                return body
        await asyncio.sleep(0.15)
    raise TimeoutError(f"execution {exec_id} did not finish in {timeout}s")


async def _drain_background() -> None:
    """Await any in-flight inline execution tasks so the loop closes cleanly."""
    tasks = [t for t in executor_mod._background_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _seed_ran_execution(client: httpx.AsyncClient, tag: str) -> tuple[str, str, dict]:
    """Create + run a tiny workflow; returns (workflow_id, execution_id, payload)."""
    payload = {"tag": tag, "n": 7}
    wf_res = await client.post(
        "/workflows",
        json={
            "name": f"v4-api-test-{tag}",
            "description": "temp workflow for v4 API tests",
            "is_active": False,
            "graph": {
                "nodes": [
                    {"id": "t", "type": "manual_trigger", "parameters": {"payload": payload}},
                    {
                        "id": "c",
                        "type": "code",
                        "parameters": {"code": "result = {'echo': input_data['payload']['tag'], 'n2': input_data['payload']['n'] * 2}\n"},
                    },
                ],
                "edges": [{"id": "e1", "source": "t", "target": "c"}],
            },
        },
    )
    assert wf_res.status_code in (200, 201), wf_res.text
    wf = wf_res.json()
    run_res = await client.post(f"/workflows/{wf['id']}/run", json={"payload": payload})
    assert run_res.status_code == 200, run_res.text
    exec_id = run_res.json()["execution_id"]
    detail = await _wait_execution(client, exec_id)
    assert detail["status"] == "success", detail
    return wf["id"], exec_id, payload


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


# ------------------------------------------------------------------ list shape
def test_list_includes_workflow_name_and_status_filter():
    tag = uuid.uuid4().hex[:8]
    created: list[str] = []

    async def _go():
        async with _client() as client:
            wf_id, exec_id, payload = await _seed_ran_execution(client, tag)
            created.append(wf_id)

            # --- list shape: workflow_name resolved
            res = await client.get("/executions", params={"workflow_id": wf_id, "limit": 10})
            assert res.status_code == 200
            rows = res.json()
            assert rows, "expected at least one execution"
            row = next(r for r in rows if r["id"] == exec_id)
            assert row["workflow_name"] == f"v4-api-test-{tag}"
            assert row["status"] == "success"
            assert row["error"] is None

            # --- status filter: success only returns successes
            res_ok = await client.get("/executions", params={"status": "success", "limit": 50})
            assert res_ok.status_code == 200
            assert all(r["status"] == "success" for r in res_ok.json())
            assert any(r["id"] == exec_id for r in res_ok.json())

            # --- status filter: impossible-status combination returns empty list
            res_err = await client.get(
                "/executions", params={"workflow_id": wf_id, "status": "error"}
            )
            assert res_err.status_code == 200
            assert res_err.json() == []

            # --- combined workflow_id + status=success works
            res_comb = await client.get(
                "/executions", params={"workflow_id": wf_id, "status": "success"}
            )
            assert res_comb.status_code == 200
            assert [r["id"] for r in res_comb.json()] == [exec_id]
        await _drain_background()

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(created))


# ------------------------------------------------------------------ rerun
def test_rerun_creates_new_success_execution_with_same_payload():
    tag = uuid.uuid4().hex[:8]
    created: list[str] = []

    async def _go():
        async with _client() as client:
            wf_id, exec_id, payload = await _seed_ran_execution(client, tag)
            created.append(wf_id)

            res = await client.post(f"/executions/{exec_id}/rerun")
            assert res.status_code == 202, res.text
            body = res.json()
            assert body["rerun_of"] == exec_id
            assert body["workflow_id"] == wf_id
            new_id = body["execution_id"]
            assert new_id != exec_id

            detail = await _wait_execution(client, new_id)
            assert detail["status"] == "success", detail
            assert detail["trigger_payload"]["payload"] == payload
            # the code node echoed the recorded payload
            code_runs = [r for r in detail["node_runs"] if r["node_id"] == "c"]
            assert code_runs and code_runs[0]["output"]["result"]["echo"] == tag
            assert code_runs[0]["output"]["result"]["n2"] == 14

            # rerun 404 for unknown id
            res_404 = await client.post("/executions/nonexistent/rerun")
            assert res_404.status_code == 404
        await _drain_background()

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(created))


# ------------------------------------------------------------------ delete
def test_delete_execution_removes_row():
    tag = uuid.uuid4().hex[:8]
    created: list[str] = []

    async def _go():
        async with _client() as client:
            wf_id, exec_id, _ = await _seed_ran_execution(client, tag)
            created.append(wf_id)

            res = await client.delete(f"/executions/{exec_id}")
            assert res.status_code == 200
            assert res.json()["ok"] is True

            gone = await client.get(f"/executions/{exec_id}")
            assert gone.status_code == 404

            # delete again → 404
            res2 = await client.delete(f"/executions/{exec_id}")
            assert res2.status_code == 404

            # list no longer contains it
            listing = await client.get("/executions", params={"workflow_id": wf_id})
            assert all(r["id"] != exec_id for r in listing.json())
        await _drain_background()

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(created))
