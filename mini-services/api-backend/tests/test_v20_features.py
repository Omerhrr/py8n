"""V20 feature tests: per-workflow retention overrides + settings API surface.

Overrides: Workflow.retention_days tri-state via PUT (omitted = untouched,
null = inherit global policy, 0 = keep forever, N = purge after N days).
The purge must honor each workflow's effective policy while leaving others
untouched. Age-based only — the global volume cap stays uniform (covered in
v19 tests).

Same harness as v4-v19: httpx ASGITransport in-process, per-test asyncio.run,
uuid-suffixed names, finally-cleanup + background drain.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

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


def _graph() -> dict:
    return {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "name": "Trigger", "position": {"x": 0, "y": 0}, "parameters": {}},
        ],
        "edges": [],
    }


async def _run_and_wait(client: httpx.AsyncClient, workflow_id: str) -> dict:
    res = await client.post(f"/workflows/{workflow_id}/run", json={"payload": {}})
    assert res.status_code in (200, 202), res.text
    exec_id = res.json()["execution_id"]
    for _ in range(80):
        res = await client.get(f"/executions/{exec_id}")
        assert res.status_code == 200, res.text
        if res.json()["status"] != "running":
            return res.json()
        await asyncio.sleep(0.05)
    raise AssertionError("execution did not finish in time")


async def _backdate(execution_id: str, days: int) -> None:
    from app.db import AsyncSessionLocal
    from app.models import ExecutionLog

    async with AsyncSessionLocal() as session:
        row = await session.get(ExecutionLog, execution_id)
        assert row is not None
        row.started_at = datetime.now(timezone.utc) - timedelta(days=days)
        row.finished_at = datetime.now(timezone.utc) - timedelta(days=days - 1)
        await session.commit()


def test_v20_per_workflow_retention_overrides():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []

    async def _go():
        async with _client() as client:
            # global policy: 30 days
            res = await client.put("/settings/retention", json={"retention_days": 30, "max_executions_per_workflow": 0})
            assert res.status_code == 200, res.text

            # three workflows: A keeps forever, B 1 day, C inherits global
            ids = {}
            for label, override in (("A", {"retention_days": 0}), ("B", {"retention_days": 1}), ("C", {})):
                res = await client.post("/workflows", json={"name": f"tmp v20 {label} {tag}", "graph": _graph()})
                assert res.status_code == 201, res.text
                wf = res.json()
                ids[label] = wf["id"]
                wf_ids.append(wf["id"])
                if override:
                    res = await client.put(f"/workflows/{wf['id']}", json=override)
                    assert res.status_code == 200, res.text
                    assert res.json()["retention_days"] == override["retention_days"]

            # absent from PUT body = untouched
            res = await client.put(f"/workflows/{ids['A']}", json={"description": "v20 settings modal"})
            assert res.status_code == 200
            assert res.json()["retention_days"] == 0
            assert res.json()["description"] == "v20 settings modal"

            # list items carry the override too
            res = await client.get("/workflows")
            listing = {w["id"]: w for w in res.json()}
            assert listing[ids["A"]]["retention_days"] == 0
            assert listing[ids["C"]]["retention_days"] is None

            # one execution each, backdated far beyond the global cutoff
            for label in ("A", "B", "C"):
                detail = await _run_and_wait(client, ids[label])
                await _backdate(detail["id"], 60)

            res = await client.post("/settings/retention/purge")
            assert res.status_code == 200, res.text

            # A (0 = keep forever) survives both global and override purge
            res = await client.get(f"/executions?workflow_id={ids['A']}")
            assert res.status_code == 200 and len(res.json()) == 1, res.json()
            # B (1 day) purged by its own override even though global is 30
            res = await client.get(f"/executions?workflow_id={ids['B']}")
            assert res.status_code == 200 and len(res.json()) == 0, res.json()
            # C (inherit) purged by the global 30-day policy
            res = await client.get(f"/executions?workflow_id={ids['C']}")
            assert res.status_code == 200 and len(res.json()) == 0, res.json()

            # back to inherit: null clears the override
            res = await client.put(f"/workflows/{ids['A']}", json={"retention_days": None})
            assert res.status_code == 200 and res.json()["retention_days"] is None

            # negative values rejected
            res = await client.put(f"/workflows/{ids['A']}", json={"retention_days": -3})
            assert res.status_code in (400, 422)

            # restore global defaults
            res = await client.put("/settings/retention", json={"retention_days": 30, "max_executions_per_workflow": 0})
            assert res.status_code == 200

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids))
