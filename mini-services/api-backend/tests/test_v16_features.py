"""V16 feature tests: workflow folders (hierarchical grouping).

Covers: folder CRUD API (create/reparent/rename/delete), nesting depth limit
(MAX_FOLDER_DEPTH=3), cycle prevention (cannot move a folder into its own
subtree), delete-refusal while subfolders exist (409), workflow assignment
(create-time folder_id, tri-state PUT - "" moves to root, unknown folder 400),
list enrichment (folder_id + folder_name, ?folder_id= filter incl. "none"),
duplicate inheriting the folder, and folder moves NOT polluting version
history (organizational change, v13 contract).

Runs the FastAPI app in-process via httpx ASGITransport against the dev
SQLite DB (same harness as v4-v15). All assertions scope to folders and
workflows created here (uuid-suffixed names) so dev data never flakes.
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


async def _cleanup(workflow_ids: list[str], folder_ids: list[str]) -> None:
    """Folders last (delete refuses while subfolders exist → leaf-first)."""
    async with _client() as client:
        for wid in workflow_ids:
            try:
                await client.delete(f"/workflows/{wid}")
            except Exception:
                pass
        for fid in reversed(folder_ids):  # children were created after parents
            try:
                await client.delete(f"/folders/{fid}")
            except Exception:
                pass
    await _drain_background()


# ------------------------------------------------------------------ API tests
def test_folders_crud_hierarchy_and_guards():
    tag = uuid.uuid4().hex[:8]
    folder_ids: list[str] = []

    async def _go():
        async with _client() as client:
            # create root folder
            res = await client.post("/folders", json={"name": f"Marketing {tag}"})
            assert res.status_code == 201, res.text
            root = res.json()
            folder_ids.append(root["id"])
            assert root["parent_id"] is None and root["workflow_count"] == 0

            # whitespace-only name → 400
            res = await client.post("/folders", json={"name": "   "})
            assert res.status_code == 400, res.text

            # unknown parent → 400
            res = await client.post("/folders", json={"name": "x", "parent_id": "nope"})
            assert res.status_code == 400, res.text

            # nest: child + grandchild (depth 2 and 3 - both allowed)
            res = await client.post("/folders", json={"name": f"Emails {tag}", "parent_id": root["id"]})
            assert res.status_code == 201, res.text
            child = res.json()
            folder_ids.append(child["id"])
            res = await client.post("/folders", json={"name": f"Deep {tag}", "parent_id": child["id"]})
            assert res.status_code == 201, res.text
            grandchild = res.json()
            folder_ids.append(grandchild["id"])

            # depth limit: great-grandchild would be level 4 → 400
            res = await client.post("/folders", json={"name": "Too deep", "parent_id": grandchild["id"]})
            assert res.status_code == 400, res.text

            # list shows all three with recursive totals
            res = await client.get("/folders")
            assert res.status_code == 200, res.text
            rows = {r["id"]: r for r in res.json()}
            assert rows[child["id"]]["parent_id"] == root["id"]
            assert rows[root["id"]]["total_count"] == 0  # no workflows yet, counts stay 0

            # rename
            res = await client.patch(f"/folders/{child['id']}", json={"name": f"Emails 2 {tag}"})
            assert res.status_code == 200 and res.json()["name"] == f"Emails 2 {tag}", res.text

            # reparent grandchild under root (move across the tree)
            res = await client.patch(f"/folders/{grandchild['id']}", json={"parent_id": root["id"]})
            assert res.status_code == 200 and res.json()["parent_id"] == root["id"], res.text

            # cycle guard: moving root under its own descendant → 400
            res = await client.patch(f"/folders/{root['id']}", json={"parent_id": grandchild["id"]})
            assert res.status_code == 400, res.text
            # self-parent guard
            res = await client.patch(f"/folders/{root['id']}", json={"parent_id": root["id"]})
            assert res.status_code == 400, res.text
            # unknown parent guard
            res = await client.patch(f"/folders/{root['id']}", json={"parent_id": "nope"})
            assert res.status_code == 400, res.text

            # delete refusal: root still has children → 409
            res = await client.delete(f"/folders/{root['id']}")
            assert res.status_code == 409, res.text

            # move grandchild back under child, then delete child → 409 again;
            # delete the LEAF first, then the branch works
            await client.patch(f"/folders/{grandchild['id']}", json={"parent_id": child["id"]})
            res = await client.delete(f"/folders/{grandchild['id']}")
            assert res.status_code == 204, res.text
            folder_ids.remove(grandchild["id"])
            res = await client.delete(f"/folders/{child['id']}")
            assert res.status_code == 204, res.text
            folder_ids.remove(child["id"])
            assert (await client.get(f"/folders/{child['id']}")).status_code == 404
    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup([], folder_ids))


def test_workflow_folder_assignment_and_lifecycle():
    tag = uuid.uuid4().hex[:8]
    folder_ids: list[str] = []
    created: list[str] = []

    async def _go():
        async with _client() as client:
            res = await client.post("/folders", json={"name": f"Wf folder {tag}"})
            assert res.status_code == 201, res.text
            folder = res.json()
            folder_ids.append(folder["id"])

            # create-time assignment
            res = await client.post("/workflows", json={
                "name": f"v16 filed {tag}", "folder_id": folder["id"],
                "graph": {"nodes": [], "edges": []},
            })
            assert res.status_code == 201, res.text
            wf = res.json()
            created.append(wf["id"])
            assert wf["folder_id"] == folder["id"]

            # unknown folder at create → 400
            res = await client.post("/workflows", json={"name": f"v16 bad {tag}", "folder_id": "nope"})
            assert res.status_code == 400, res.text

            # list enrichment: folder_id + folder_name resolved
            res = await client.get("/workflows")
            row = next(r for r in res.json() if r["id"] == wf["id"])
            assert row["folder_id"] == folder["id"] and row["folder_name"] == f"Wf folder {tag}", row

            # server-side filter: by id and by "none"
            res = await client.get(f"/workflows?folder_id={folder['id']}")
            assert any(r["id"] == wf["id"] for r in res.json())
            res = await client.get("/workflows?folder_id=none")
            assert all(r["id"] != wf["id"] for r in res.json())

            # duplicate inherits the folder
            res = await client.post(f"/workflows/{wf['id']}/duplicate")
            assert res.status_code == 201, res.text
            dup = res.json()
            created.append(dup["id"])
            assert dup["folder_id"] == folder["id"]

            # tri-state PUT: "" moves to root; unknown id → 400
            res = await client.put(f"/workflows/{wf['id']}", json={"folder_id": ""})
            assert res.status_code == 200 and res.json()["folder_id"] is None, res.text
            res = await client.put(f"/workflows/{wf['id']}", json={"folder_id": "nope"})
            assert res.status_code == 400, res.text
            res = await client.put(f"/workflows/{wf['id']}", json={"folder_id": folder["id"]})
            assert res.json()["folder_id"] == folder["id"]

            # folder moves are organizational - NO new version (v13 contract)
            res = await client.get(f"/workflows/{wf['id']}/versions")
            assert res.json()["latest"] == 1, res.json()

            # delete folder → workflows fall back to root
            res = await client.delete(f"/folders/{folder['id']}")
            assert res.status_code == 204, res.text
            folder_ids.remove(folder["id"])
            detail = (await client.get(f"/workflows/{wf['id']}")).json()
            assert detail["folder_id"] is None, detail
            dup_row = (await client.get(f"/workflows/{dup['id']}")).json()
            assert dup_row["folder_id"] is None
    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(created, folder_ids))
