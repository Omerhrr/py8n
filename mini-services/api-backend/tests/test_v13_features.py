"""V13 feature tests: workflow version history & restore.

Covers: automatic v1 snapshot on create, new version per content change
(graph / name), NO version for organizational changes (tags / error
binding), version detail retrieval, restore semantics (content rolled back,
restore lands as a NEW version - nothing destroyed), 404 guards, and the
20-snapshot retention cap with pruning.

Runs the FastAPI app in-process via httpx ASGITransport against the dev
SQLite DB (same harness as v4-v12).
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


def _graph(node_count: int, marker: str = "n") -> dict:
    return {
        "nodes": [
            {"id": f"{marker}{i}", "type": "manual_trigger" if i == 0 else "set_variable",
             "name": f"N{i}", "parameters": {}}
            for i in range(node_count)
        ],
        "edges": [],
    }


async def _create(client: httpx.AsyncClient, name: str) -> dict:
    res = await client.post("/workflows", json={"name": name, "graph": _graph(1)})
    assert res.status_code == 201, res.text
    return res.json()


def test_version_lifecycle_and_restore():
    tag = uuid.uuid4().hex[:8]
    created: list[str] = []

    async def _go():
        async with _client() as client:
            wf = await _create(client, f"v13 hist {tag}")
            created.append(wf["id"])
            original_name = wf["name"]

            # create snapshot the initial state as v1
            res = await client.get(f"/workflows/{wf['id']}/versions")
            assert res.status_code == 200, res.text
            hist = res.json()
            assert hist["latest"] == 1 and hist["max_versions"] == 20
            assert len(hist["versions"]) == 1
            assert hist["versions"][0]["version"] == 1
            assert hist["versions"][0]["is_current"] is True
            assert hist["versions"][0]["node_count"] == 1

            # content change → v2
            res = await client.put(f"/workflows/{wf['id']}", json={"graph": _graph(3)})
            assert res.status_code == 200
            hist = (await client.get(f"/workflows/{wf['id']}/versions")).json()
            assert hist["latest"] == 2 and len(hist["versions"]) == 2
            assert hist["versions"][0]["is_current"] is True
            assert hist["versions"][1]["is_current"] is False

            # organizational changes create NO version
            await client.put(f"/workflows/{wf['id']}", json={"tags": ["hist"]})
            await client.put(f"/workflows/{wf['id']}", json={"error_workflow_id": ""})
            hist = (await client.get(f"/workflows/{wf['id']}/versions")).json()
            assert hist["latest"] == 2, "tags/error-binding must not snapshot"

            # rename → v3
            await client.put(f"/workflows/{wf['id']}", json={"name": f"v13 renamed {tag}"})
            hist = (await client.get(f"/workflows/{wf['id']}/versions")).json()
            assert hist["latest"] == 3

            # version detail: v1 keeps the original 1-node graph + name
            res = await client.get(f"/workflows/{wf['id']}/versions/1")
            assert res.status_code == 200, res.text
            snap = res.json()
            assert snap["version"] == 1 and snap["node_count"] == 1
            assert snap["name"] == original_name
            assert len(snap["graph"]["nodes"]) == 1

            # restore v1 → content back, restore lands as v4 (history intact)
            res = await client.post(f"/workflows/{wf['id']}/versions/1/restore")
            assert res.status_code == 200, res.text
            restored = res.json()
            assert restored["name"] == original_name
            assert len(restored["graph"]["nodes"]) == 1

            hist = (await client.get(f"/workflows/{wf['id']}/versions")).json()
            assert hist["latest"] == 4, "restore must append, not truncate"
            assert [v["version"] for v in hist["versions"]] == [4, 3, 2, 1]
            assert hist["versions"][0]["is_current"] is True
            # tags survived the restore (organizational metadata untouched)
            row = (await client.get(f"/workflows/{wf['id']}")).json()
            assert row["tags"] == ["hist"], row["tags"]

            # 404 guards
            assert (await client.get("/workflows/nope/versions")).status_code == 404
            assert (await client.get(f"/workflows/{wf['id']}/versions/99")).status_code == 404
            assert (await client.post(f"/workflows/{wf['id']}/versions/99/restore")).status_code == 404
            assert (await client.post("/workflows/nope/versions/1/restore")).status_code == 404
    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(created))


def test_version_retention_cap_prunes_oldest():
    tag = uuid.uuid4().hex[:8]
    created: list[str] = []

    async def _go():
        async with _client() as client:
            wf = await _create(client, f"v13 cap {tag}")
            created.append(wf["id"])

            # v1 on create + 22 content saves → 23 versions ever, 20 kept
            for i in range(22):
                res = await client.put(
                    f"/workflows/{wf['id']}",
                    json={"description": f"gen {i}", "graph": _graph(1 + (i % 3))},
                )
                assert res.status_code == 200, res.text

            hist = (await client.get(f"/workflows/{wf['id']}/versions")).json()
            assert len(hist["versions"]) == 20, len(hist["versions"])
            assert hist["latest"] == 23
            # oldest survivors are exactly the last 20 versions
            assert [v["version"] for v in hist["versions"]] == list(range(23, 3, -1))
            assert hist["versions"][0]["is_current"] is True

            # delete cascade: versions die with the workflow (no orphans)
            from app.db import AsyncSessionLocal
            from app.models import WorkflowVersion
            from sqlalchemy import select, func as sa_func

            async with AsyncSessionLocal() as session:
                await client.delete(f"/workflows/{wf['id']}")
                created.remove(wf["id"])
                remaining = (
                    await session.execute(
                        select(sa_func.count())
                        .select_from(WorkflowVersion)
                        .where(WorkflowVersion.workflow_id == wf["id"])
                    )
                ).scalar()
                assert remaining == 0, f"orphaned versions: {remaining}"
    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(created))
