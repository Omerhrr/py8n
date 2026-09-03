"""V61 feature tests: Py8n Systems.

THE OPERATING UNIT ABOVE WORKFLOWS: a system binds workflows + datasets
+ apps + dashboards + models + reports into one named, health-scored,
ownable unit.

- Attach validation resolves every reference against the live table with
  owner scoping: unknown kinds 400, missing/foreign objects 404,
  duplicates 409; a dissolved system leaves its member objects intact.
- The health verdict is DERIVED from the members at read time: 7d run
  failures, dataset health tiers (budget-capped), report delivery
  outcomes - degraded with any failure, unhealthy on catastrophic
  ratios.
- Bridges: a Marketplace solution installs as a system
  (POST /solutions/{slug}/install {"as_system": true}), and the AI
  System Builder's build step can wrap its created primitives into a
  system (POST /builder/systems/{id}/build {"as_system": true}).

Runs the FastAPI app in-process via httpx ASGITransport (same harness as v4-v60).
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


async def _mk_user(client: httpx.AsyncClient, tag: str, n: int) -> dict:
    res = await client.post("/auth/register", json={
        "email": f"v61-{tag}-u{n}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v61 u{n} {tag}",
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


def _node(nid: str, ntype: str, params: dict | None = None) -> dict:
    return {"id": nid, "type": ntype, "name": nid, "position": {"x": 0, "y": 0}, "parameters": params or {}}


def _edge(eid: str, source: str, target: str) -> dict:
    return {"id": eid, "source": source, "target": target, "sourceHandle": "main", "targetHandle": "main"}


def test_v61_systems_core():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"sys-{tag}", 1)
            h = _auth(user["token"])

            # member objects: a healthy workflow, a failing workflow, a dataset
            res = await client.post("/datasets", headers=h, json={"name": f"sys-ds-{tag}", "rows": [{"a": 1}]})
            ds = res.json()
            ok_graph = {"nodes": [_node("t1", "manual_trigger"),
                                  _node("gen", "code", {"code": "result = {'ok': 1}"})],
                        "edges": [_edge("e1", "t1", "gen")]}
            res = await client.post("/workflows", headers=h, json={"name": "sys-ok", "graph": ok_graph})
            wf_ok = res.json()
            bad_graph = {"nodes": [_node("t1", "manual_trigger"),
                                   _node("boom", "code", {"code": "result = 1 / 0"})],
                         "edges": [_edge("e1", "t1", "boom")]}
            res = await client.post("/workflows", headers=h, json={"name": "sys-bad", "graph": bad_graph})
            wf_bad = res.json()
            await _run_and_wait(client, wf_ok["id"], h)
            await _run_and_wait(client, wf_bad["id"], h)

            # --- create + attach ---------------------------------------------
            res = await client.post("/systems", headers=h, json={
                "name": f"Customer Ops {tag}", "description": "the unit that runs support"})
            assert res.status_code == 201, res.text
            sys_row = res.json()
            assert sys_row["total_components"] == 0

            res = await client.post(f"/systems/{sys_row['id']}/components", headers=h,
                                    json={"kind": "workflow", "ref_id": wf_ok["id"]})
            assert res.status_code == 201, res.text
            res = await client.post(f"/systems/{sys_row['id']}/components", headers=h,
                                    json={"kind": "workflow", "ref_id": wf_bad["id"]})
            assert res.status_code == 201
            res = await client.post(f"/systems/{sys_row['id']}/components", headers=h,
                                    json={"kind": "dataset", "ref_id": ds["id"]})
            assert res.status_code == 201

            # attach validation
            res = await client.post(f"/systems/{sys_row['id']}/components", headers=h,
                                    json={"kind": "gadget", "ref_id": wf_ok["id"]})
            assert res.status_code == 400
            res = await client.post(f"/systems/{sys_row['id']}/components", headers=h,
                                    json={"kind": "workflow", "ref_id": "missing-id"})
            assert res.status_code == 404
            res = await client.post(f"/systems/{sys_row['id']}/components", headers=h,
                                    json={"kind": "workflow", "ref_id": wf_ok["id"]})
            assert res.status_code == 409  # already bound

            # --- detail + derived health --------------------------------------
            res = await client.get(f"/systems/{sys_row['id']}", headers=h)
            assert res.status_code == 200, res.text
            detail = res.json()
            assert len(detail["grouped"]["workflow"]) == 2
            assert detail["grouped"]["workflow"][0]["name"] == "sys-ok"
            assert detail["health"]["workflows"]["runs_7d"] >= 2
            assert detail["health"]["workflows"]["failures_7d"] >= 1
            assert detail["health"]["verdict"] == "degraded"
            assert detail["health"]["datasets"]["total"] == 1
            assert detail["health"]["datasets"]["healthy"] == 1

            # --- list shows the card with a verdict ---------------------------
            res = await client.get("/systems", headers=h)
            card = next(s for s in res.json() if s["id"] == sys_row["id"])
            assert card["total_components"] == 3
            assert card["verdict"] in ("healthy", "degraded", "unhealthy")

            # --- detach + dissolve semantics ------------------------------------
            comp_id = detail["grouped"]["workflow"][0]["component_id"]
            res = await client.delete(f"/systems/{sys_row['id']}/components/{comp_id}", headers=h)
            assert res.status_code == 204
            res = await client.get(f"/workflows/{wf_ok['id']}", headers=h)
            assert res.status_code == 200  # unbinding never deletes the object
            res = await client.delete(f"/systems/{sys_row['id']}/components/{comp_id}", headers=h)
            assert res.status_code == 404

            res = await client.delete(f"/systems/{sys_row['id']}", headers=h)
            assert res.status_code == 204
            res = await client.get("/systems", headers=h)
            assert not any(s["id"] == sys_row["id"] for s in res.json())
            # members survive the dissolve
            res = await client.get(f"/workflows/{wf_bad['id']}", headers=h)
            assert res.status_code == 200
            res = await client.get(f"/datasets/{ds['id']}", headers=h)
            assert res.status_code == 200

            # --- scoping ---------------------------------------------------------
            res = await client.post("/systems", headers=h, json={"name": f"S2 {tag}"})
            s2 = res.json()
            other = await _mk_user(client, f"sys-{tag}", 2)
            res = await client.get(f"/systems/{s2['id']}", headers=_auth(other["token"]))
            assert res.status_code == 404
            res = await client.delete(f"/systems/{s2['id']}", headers=_auth(other["token"]))
            assert res.status_code == 404
            # a foreign object cannot be bound (looks nonexistent)
            res = await client.post(f"/systems/{s2['id']}/components", headers=_auth(other["token"]),
                                    json={"kind": "dataset", "ref_id": ds["id"]})
            assert res.status_code == 404

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


def test_v61_system_bridges():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"bridge-{tag}", 1)
            h = _auth(user["token"])

            # --- marketplace -> system ------------------------------------------
            res = await client.post("/solutions/invoice-processing/install", headers=h,
                                    json={"as_system": True})
            assert res.status_code in (200, 201), res.text
            installed = res.json()
            assert installed["system"] and installed["system"]["name"] == "Invoice Processing system"
            sys_id = installed["system"]["id"]
            res = await client.get(f"/systems/{sys_id}", headers=h)
            detail = res.json()
            assert detail["components"]["workflow"] == 1
            assert detail["components"]["dataset"] == 2
            # the bound workflow runs (fully offline)
            wf_id = detail["grouped"]["workflow"][0]["ref_id"]
            run = await _run_and_wait(client, wf_id, h)
            assert run["status"] == "success", run
            # system health now reflects the member run
            res = await client.get(f"/systems/{sys_id}", headers=h)
            assert res.json()["health"]["workflows"]["runs_7d"] >= 1
            assert res.json()["health"]["verdict"] in ("healthy", "degraded")

            # --- builder -> system ----------------------------------------------
            res = await client.post("/builder/systems", headers=h, json={
                "description": "every day pull sales from sqlite, validate the schema, dedupe and write to a curated dataset"})
            assert res.status_code == 201, res.text
            d = res.json()
            res = await client.post(f"/builder/systems/{d['id']}/answers", headers=h, json={"answers": {
                "table": "users", "fields": "id:text, email:text", "dedupe_keys": "id"}})
            assert res.status_code == 200, res.text
            res = await client.post(f"/builder/systems/{d['id']}/build", headers=h, json={"as_system": True})
            assert res.status_code == 200, res.text
            built = res.json()["built"]
            assert built.get("system_id")
            res = await client.get(f"/systems/{built['system_id']}", headers=h)
            sys2 = res.json()
            assert sys2["components"]["workflow"] == 1
            assert sys2["components"]["dataset"] == 1
            assert sys2["components"]["dashboard"] == 0  # the ask didn't call for one

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())
