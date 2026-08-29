"""V12 feature tests: workflow tags + search.

Covers: tag normalization (trim/whitespace-collapse/lowercase/dedupe, 10×32
cap), tri-state PUT semantics (omitted = untouched, [] = clear), GET
/workflows?tag= + ?search= filters, GET /workflows/tags vocabulary summary,
duplicate carrying tags, and 404 on unknown workflow.

Runs the FastAPI app in-process via httpx ASGITransport against the dev
SQLite DB (same harness as v4-v11). Assertions scope to workflows created
here with uuid-tagged names so pre-existing dev data never flakes a run.
"""

from __future__ import annotations

import asyncio
import uuid

import httpx

from app.main import app

API = "http://testserver/api/v1"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _cleanup(workflow_ids: list[str]) -> None:
    if not workflow_ids:
        return
    async with _client() as client:
        for wid in workflow_ids:
            try:
                await client.delete(f"/workflows/{wid}")
            except Exception:
                pass


def _graph() -> dict:
    return {
        "nodes": [{"id": "t1", "type": "manual_trigger", "name": "Manual", "parameters": {}}],
        "edges": [],
    }


async def _create(client: httpx.AsyncClient, name: str, tags: list[str] | None = None,
                  description: str = "") -> dict:
    body: dict = {"name": name, "graph": _graph(), "is_active": False, "description": description}
    if tags is not None:
        body["tags"] = tags
    res = await client.post("/workflows", json=body)
    assert res.status_code == 201, res.text
    return res.json()


def test_tags_normalization_and_tri_state_update():
    tag = uuid.uuid4().hex[:8]
    created: list[str] = []

    async def _go():
        async with _client() as client:
            wf = await _create(client, f"v12-norm-{tag}", tags=["  Prod ", "AI", "ai", "", 123, "x" * 40])
            created.append(wf["id"])
            # junk dropped, whitespace collapsed, lowercased, deduped, capped
            assert wf["tags"] == ["prod", "ai", "x" * 32], wf["tags"]

            # replace via PUT
            res = await client.put(f"/workflows/{wf['id']}", json={"tags": ["Demo", " demo ", "etl"]})
            assert res.status_code == 200, res.text
            assert res.json()["tags"] == ["demo", "etl"], res.json()["tags"]

            # omitted tags on PUT = untouched (tri-state)
            res = await client.put(f"/workflows/{wf['id']}", json={"description": "still tagged"})
            assert res.json()["tags"] == ["demo", "etl"]

            # [] clears all tags
            res = await client.put(f"/workflows/{wf['id']}", json={"tags": []})
            assert res.status_code == 200 and res.json()["tags"] == [], res.text

            # create without tags defaults to []
            wf_none = await _create(client, f"v12-untagged-{tag}")
            created.append(wf_none["id"])
            assert wf_none["tags"] == []

            # unknown workflow → 404
            res = await client.put("/workflows/does-not-exist", json={"tags": ["x"]})
            assert res.status_code == 404
        # duplicate carries tags
        async with _client() as client:
            src = await _create(client, f"v12-dup-{tag}", tags=["keepme"])
            created.append(src["id"])
            res = await client.post(f"/workflows/{src['id']}/duplicate")
            assert res.status_code == 201, res.text
            copy = res.json()
            created.append(copy["id"])
            assert copy["tags"] == ["keepme"], copy["tags"]
        # vocabulary summary is reachable and counts source + its duplicate
        async with _client() as client:
            res = await client.get("/workflows/tags")
            assert res.status_code == 200, res.text
            vocab = {row["tag"]: row["count"] for row in res.json()}
            assert vocab.get("keepme") == 2, vocab  # src + copy both carry it
    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(created))


def test_tags_filter_and_search():
    tag = uuid.uuid4().hex[:8]
    created: list[str] = []

    async def _go():
        async with _client() as client:
            wf_a = await _create(client, f"v12 Alpha {tag}", tags=["prod", "etl"],
                                 description="nightly numbers cruncher")
            created.append(wf_a["id"])
            wf_b = await _create(client, f"v12 Beta {tag}", tags=["dev"])
            created.append(wf_b["id"])

            # tag filter (case-insensitive on the query too)
            res = await client.get("/workflows", params={"tag": "PROD"})
            rows = res.json()
            mine = [r for r in rows if r["id"] in (wf_a["id"], wf_b["id"])]
            assert [r["id"] for r in mine] == [wf_a["id"]], rows
            assert mine[0]["tags"] == ["prod", "etl"]

            # unknown tag → empty result
            res = await client.get("/workflows", params={"tag": "no-such-tag-xyz"})
            assert res.json() == []

            # search hits the name…
            res = await client.get("/workflows", params={"search": f"alpha {tag}"})
            assert [r["id"] for r in res.json()] == [wf_a["id"]]
            # …and the description
            res = await client.get("/workflows", params={"search": "numbers cruncher"})
            assert wf_a["id"] in {r["id"] for r in res.json()}
            assert wf_b["id"] not in {r["id"] for r in res.json()}

            # combined tag + search narrows further
            res = await client.get("/workflows", params={"tag": "prod", "search": "beta"})
            assert res.json() == []
    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(created))
