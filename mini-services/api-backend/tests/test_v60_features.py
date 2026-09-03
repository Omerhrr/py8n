"""V60 feature tests: the Solution Marketplace.

GALLERY + PACKS -> OUTCOME-NAMED SOLUTIONS: a Solution is a thin
outcome-named shell around a standard py8n-pack document - the shelf
says "Customer Support Automation" and shows WHAT YOU GET (the
capability checklist) instead of node names.

- The shelf self-seeds the three roadmap showcase solutions
  (Customer Support Automation / Invoice Processing / API Monitoring)
  idempotently by slug, and every curated graph passes the same
  validate_graph_document gate user workflows go through.
- Installing reuses the exact pack-import machinery: workflows land
  inactive, datasets carry sample rows, installs increment.
- Authoring is two-way: publish your own workflows/datasets as a
  solution; only the author may unlist it.

Runs the FastAPI app in-process via httpx ASGITransport (same harness as v4-v59).
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
        "email": f"v60-{tag}-u{n}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v60 u{n} {tag}",
    })
    assert res.status_code == 201, res.text
    body = res.json()
    return {"token": body["token"], "id": body["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_v60_marketplace():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"mkt-{tag}", 1)
            h = _auth(user["token"])

            # --- the shelf self-seeds the three showcase solutions ----------
            res = await client.get("/solutions", headers=h)
            assert res.status_code == 200, res.text
            shelf = res.json()
            slugs = {s["slug"] for s in shelf["solutions"]}
            assert {"customer-support-automation", "invoice-processing", "api-monitoring"} <= slugs
            support = next(s for s in shelf["solutions"] if s["slug"] == "customer-support-automation")
            assert support["curated"] is True
            assert "Ticket intake" in support["outcomes"]
            assert "AI classification" in support["outcomes"]
            assert support["workflow_count"] == 1 and support["dataset_count"] >= 1
            assert "Support" in shelf["categories"]
            # idempotent: a second read doesn't duplicate
            res = await client.get("/solutions", headers=h)
            assert len(res.json()["solutions"]) == len(shelf["solutions"])

            # --- detail: capability checklist + pack summary ------------------
            res = await client.get("/solutions/customer-support-automation", headers=h)
            assert res.status_code == 200, res.text
            detail = res.json()
            assert detail["docs"]
            assert detail["pack"]["workflows"][0]["name"] == "Support Ticket Triage"
            assert {"llm_chat", "dataset_write", "code"} <= set(detail["pack"]["node_types"])
            assert detail["pack"]["datasets"][0]["rows"] >= 2

            # --- category filter -----------------------------------------------
            res = await client.get("/solutions?category=Finance", headers=h)
            cats = {s["category"] for s in res.json()["solutions"]}
            assert cats == {"Finance"}
            res = await client.get("/solutions?q=uptime", headers=h)
            assert any(s["slug"] == "api-monitoring" for s in res.json()["solutions"])

            # --- install: pack machinery, inactive workflows, counters ---------
            res = await client.post("/solutions/customer-support-automation/install", headers=h, json={})
            assert res.status_code in (200, 201), res.text
            installed = res.json()
            assert installed["installs"] == 1
            assert len(installed["created_workflows"]) == 1
            assert len(installed["created_datasets"]) == 1  # support_tickets with 2 sample rows
            assert not installed.get("skipped")
            wf = installed["created_workflows"][0]
            ds = installed["created_datasets"][0]
            assert ds["name"] == "support_tickets"

            # the installed workflow is REAL, inactive, and runs offline
            res = await client.get(f"/workflows/{wf['id']}", headers=h)
            assert res.status_code == 200, res.text
            assert res.json()["is_active"] is False  # pack semantics
            types = [n["type"] for n in res.json()["graph"]["nodes"]]
            assert "manual_trigger" in types and "llm_chat" in types

            # second install increments the counter
            res = await client.post("/solutions/invoice-processing/install", headers=h, json={})
            res = await client.get("/solutions/invoice-processing", headers=h)
            assert res.json()["installs"] == 1
            res = await client.get("/solutions/customer-support-automation", headers=h)
            assert res.json()["installs"] == 1

            # --- authoring: publish your own content as a solution -------------
            res = await client.post("/datasets", headers=h, json={
                "name": f"mkt-ds-{tag}", "rows": [{"a": 1}, {"a": 2}]})
            ds_id = res.json()["id"]
            res = await client.post("/workflows", headers=h, json={
                "name": f"My Monitor {tag}",
                "graph": {"nodes": [
                    {"id": "t1", "type": "manual_trigger", "name": "t", "position": {"x": 0, "y": 0}, "parameters": {}},
                ], "edges": []}})
            wf_id = res.json()["id"]

            res = await client.post("/solutions", headers=h, json={
                "name": f"Team Ops Kit {tag}",
                "tagline": "Our own ops starter",
                "category": "Operations",
                "outcomes": ["Monitoring", "Alerting", "One dataset"],
                "workflow_ids": [wf_id],
                "dataset_ids": [ds_id],
            })
            assert res.status_code == 201, res.text
            authored = res.json()
            assert authored["curated"] is False
            assert authored["outcomes"] == ["Monitoring", "Alerting", "One dataset"]
            assert authored["workflow_count"] == 1

            # appears on the shelf; foreign users see it but cannot unlist
            res = await client.get("/solutions", headers=h)
            assert any(s["slug"] == authored["slug"] for s in res.json()["solutions"])
            other = await _mk_user(client, f"mkt-{tag}", 2)
            res = await client.delete(f"/solutions/{authored['slug']}", headers=_auth(other["token"]))
            assert res.status_code == 404
            # the author can unlist
            res = await client.delete(f"/solutions/{authored['slug']}", headers=h)
            assert res.status_code == 204
            res = await client.get("/solutions", headers=h)
            assert not any(s["slug"] == authored["slug"] for s in res.json()["solutions"])

            # authoring guards
            res = await client.post("/solutions", headers=h, json={
                "name": "Empty", "outcomes": ["x"]})
            assert res.status_code == 400
            res = await client.post("/solutions", headers=h, json={
                "name": "No outcomes", "workflow_ids": [wf_id], "outcomes": []})
            assert res.status_code in (400, 422)
            res = await client.get("/solutions/nope-not-here", headers=h)
            assert res.status_code == 404

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())
