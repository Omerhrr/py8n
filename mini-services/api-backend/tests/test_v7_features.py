"""V7 feature tests: automation lifecycle — schedules introspection + activation.

Covers: GET /workflows/{id}/schedule (fire-time previews), POST activate /
deactivate with pre-flight validation, save-time cron rejection, workflow-list
schedule fields, and the global GET /schedules overview.

Runs the FastAPI app in-process via httpx ASGITransport against the dev SQLite
DB (same harness as the v4 tests). APScheduler is not started in-process, so
resync calls are no-ops — pure API-level behaviour is exercised.
"""

from __future__ import annotations

import asyncio
import uuid

import httpx

from app.db import AsyncSessionLocal
from app.main import app
from app.models import Workflow
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


def _sched_graph(cron: str = "*/7 * * * *", node_id: str = "s1") -> dict:
    return {
        "nodes": [
            {
                "id": node_id,
                "type": "schedule_trigger",
                "name": "Every 7 min",
                "parameters": {"mode": "cron", "cron": cron},
            },
        ],
        "edges": [],
    }


async def _create_schedule_workflow(client: httpx.AsyncClient, tag: str, is_active: bool, cron: str = "*/7 * * * *") -> str:
    res = await client.post(
        "/workflows",
        json={
            "name": f"v7-api-test-{tag}",
            "description": "temp workflow for v7 schedule tests",
            "is_active": is_active,
            "graph": _sched_graph(cron),
        },
    )
    assert res.status_code == 201, res.text
    return res.json()["id"]


# ------------------------------------------------------- schedule introspection
def test_schedule_endpoint_returns_ascending_fire_previews():
    tag = uuid.uuid4().hex[:8]
    created: list[str] = []

    async def _go():
        async with _client() as client:
            wf_id = await _create_schedule_workflow(client, tag, is_active=False)
            created.append(wf_id)

            res = await client.get(f"/workflows/{wf_id}/schedule")
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["workflow_id"] == wf_id
            assert body["is_active"] is False
            assert body["next_run_at"] is None  # paused → no upcoming fire
            assert len(body["schedules"]) == 1
            entry = body["schedules"][0]
            assert entry["node_id"] == "s1"
            assert entry["mode"] == "cron"
            assert entry["cron"] == "*/7 * * * *"
            assert entry["summary"] == "cron */7 * * * *"
            assert entry["error"] is None
            assert len(entry["next_runs"]) == 5
            # strictly ascending, ISO strings with the same UTC offset
            assert entry["next_runs"] == sorted(entry["next_runs"])
            assert len(set(entry["next_runs"])) == 5, "fire previews must advance"

            # unknown workflow → 404
            res_404 = await client.get("/workflows/does-not-exist/schedule")
            assert res_404.status_code == 404
        await _drain_background()

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(created))


# ------------------------------------------------------- activate / deactivate
def test_activate_deactivate_roundtrip_and_404():
    tag = uuid.uuid4().hex[:8]
    created: list[str] = []

    async def _go():
        async with _client() as client:
            wf_id = await _create_schedule_workflow(client, tag, is_active=False)
            created.append(wf_id)

            res = await client.post(f"/workflows/{wf_id}/activate")
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["is_active"] is True
            assert body["next_run_at"] is not None  # active → soonest fire known

            # list reflects activation with schedule fields
            listing = await client.get("/workflows")
            item = next(w for w in listing.json() if w["id"] == wf_id)
            assert item["is_active"] is True
            assert item["schedule_summary"] == "cron */7 * * * *"
            assert item["next_run_at"] is not None

            res_off = await client.post(f"/workflows/{wf_id}/deactivate")
            assert res_off.status_code == 200
            assert res_off.json()["is_active"] is False
            assert res_off.json()["next_run_at"] is None

            assert (await client.post("/workflows/does-not-exist/activate")).status_code == 404
            assert (await client.post("/workflows/does-not-exist/deactivate")).status_code == 404
        await _drain_background()

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(created))


# ------------------------------------------------------- pre-flight guard
def test_activate_rejects_unschedulable_cron():
    tag = uuid.uuid4().hex[:8]
    created: list[str] = []

    async def _go():
        async with _client() as client:
            wf_id = await _create_schedule_workflow(client, tag, is_active=False)
            created.append(wf_id)

            # save-time validation: bad cron on update → 400
            bad = _sched_graph(cron="banana")
            res_bad = await client.put(f"/workflows/{wf_id}", json={"graph": bad})
            assert res_bad.status_code == 400
            assert "Schedule node" in res_bad.json()["detail"]

            # create-time validation: bad cron → 400
            res_create = await client.post(
                "/workflows",
                json={"name": f"v7-bad-{tag}", "graph": _sched_graph(cron="not a cron")},
            )
            assert res_create.status_code == 400

            # pre-flight on activate: plant an invalid graph directly in the DB
            # (simulates a pre-v7 row or an out-of-band edit) and try to activate
            async with AsyncSessionLocal() as session:
                row = await session.get(Workflow, wf_id)
                row.graph = _sched_graph(cron="banana")
                await session.commit()

            res_act = await client.post(f"/workflows/{wf_id}/activate")
            assert res_act.status_code == 400, res_act.text
            assert "activate" in res_act.json()["detail"].lower()

            # interval validation: non-numeric seconds → 400 on save
            res_int = await client.post(
                "/workflows",
                json={
                    "name": f"v7-bad-int-{tag}",
                    "graph": {
                        "nodes": [
                            {
                                "id": "s1",
                                "type": "schedule_trigger",
                                "parameters": {"mode": "interval", "interval_seconds": "soon"},
                            }
                        ],
                        "edges": [],
                    },
                },
            )
            assert res_int.status_code == 400
        await _drain_background()

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(created))


# ------------------------------------------------------- global schedules view
def test_global_schedules_orders_active_first_with_names():
    tag = uuid.uuid4().hex[:8]
    created: list[str] = []

    async def _go():
        async with _client() as client:
            active_id = await _create_schedule_workflow(client, f"{tag}-a", is_active=True, cron="* * * * *")
            created.append(active_id)
            paused_id = await _create_schedule_workflow(client, f"{tag}-p", is_active=False, cron="0 3 * * *")
            created.append(paused_id)

            res = await client.get("/schedules")
            assert res.status_code == 200, res.text
            rows = res.json()
            mine = [r for r in rows if r["workflow_id"] in (active_id, paused_id)]
            assert len(mine) == 2
            active_row = next(r for r in mine if r["workflow_id"] == active_id)
            paused_row = next(r for r in mine if r["workflow_id"] == paused_id)
            assert active_row["workflow_name"] == f"v7-api-test-{tag}-a"
            assert active_row["is_active"] is True
            assert active_row["next_runs"], "active cron * * * * * must preview fires"
            assert paused_row["is_active"] is False
            assert paused_row["summary"] == "cron 0 3 * * *"
            # ordering: active entry sorts before the paused one
            assert rows.index(active_row) < rows.index(paused_row)
        await _drain_background()

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(created))
