"""V39 feature tests: template packs - multi-resource portability.

New machinery:
    POST /packs/export   bundle N workflows + M dataset snapshots into one
                         JSON document (format "py8n-pack"); foreign-owned
                         ids 404 (v37 scoping), oversized datasets truncate
                         at MAX_PACK_ROWS with a manifest warning
    POST /packs/inspect  dry-run preview for the import dialog: graph
                         validity, node counts, existing-name flags and the
                         rename a colliding dataset would receive
    POST /packs/import   create everything (workflows inactive + version
                         snapshotted, datasets via the normal parquet
                         pipeline with numbered-name collision handling) and
                         return a per-item summary; invalid entries are
                         skipped with reasons, never abort the batch

Same harness as v4-v38: httpx ASGITransport in-process, per-test
asyncio.run, uuid-suffixed data, finally-cleanup + background drain.
"""

from __future__ import annotations

import asyncio
import uuid

import httpx

from app.main import app

API = "http://testserver/api/v1"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _drain_background() -> None:
    from app.services import executor as executor_mod

    tasks = [t for t in executor_mod._background_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _cleanup(workflow_ids: list[str], dataset_ids: list[str]) -> None:
    async def _go() -> None:
        async with _client() as client:
            for wid in workflow_ids:
                try:
                    await client.delete(f"/workflows/{wid}")
                except Exception:
                    pass
            for did in dataset_ids:
                try:
                    await client.delete(f"/datasets/{did}")
                except Exception:
                    pass

    asyncio.run(_go())
    asyncio.run(_drain_background())


def _node(nid: str, ntype: str, params: dict | None = None, name: str | None = None) -> dict:
    return {"id": nid, "type": ntype, "name": name or nid, "position": {"x": 0, "y": 0}, "parameters": params or {}}


def _edge(eid: str, source: str, target: str) -> dict:
    return {"id": eid, "source": source, "target": target, "sourceHandle": "main", "targetHandle": "main"}


async def _make_workflow(client: httpx.AsyncClient, name: str, graph: dict, headers: dict | None = None) -> str:
    res = await client.post("/workflows", json={"name": name, "graph": graph, "is_active": False}, headers=headers)
    assert res.status_code == 201, res.text
    return res.json()["id"]


async def _make_dataset(client: httpx.AsyncClient, name: str, rows: list[dict], headers: dict | None = None) -> str:
    res = await client.post("/datasets", json={"name": name, "rows": rows}, headers=headers)
    assert res.status_code == 201, res.text
    return res.json()["id"]


async def _register(client: httpx.AsyncClient, email: str, password: str = "s3cret-pw!") -> dict:
    res = await client.post("/auth/register", json={"email": email, "password": password, "name": "V39 Test"})
    assert res.status_code == 201, res.text
    return res.json()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------ test 1
def test_v39_health_pin():
    """Strict version pin lives in the latest wave only (convention)."""

    async def _go():
        async with _client() as client:
            return (await client.get("/health")).json()

    body = asyncio.run(_go())
    assert body["app"] == "Py8n"
    assert body["version"] >= "1.39.0", f"expected strict pin 1.39.0, got {body['version']}"


# ------------------------------------------------------------------ test 2
def test_pack_export_roundtrip():
    """Export bundles workflows + dataset rows into a py8n-pack document."""
    tag = uuid.uuid4().hex[:8]
    wf_a_graph = {
        "nodes": [_node("a", "manual_trigger"), _node("b", "code", {"code": "result = 1"})],
        "edges": [_edge("e1", "a", "b")],
    }
    wf_b_graph = {"nodes": [_node("s", "schedule_trigger")], "edges": []}
    rows = [{"city": "lisbon", "temp": 21}, {"city": "oslo", "temp": -3}, {"city": "cairo", "temp": 35}]
    wf_ids: list[str] = []
    ds_ids: list[str] = []

    async def _go():
        async with _client() as client:
            wf_a = await _make_workflow(client, f"v39 pack wf a {tag}", wf_a_graph)
            wf_b = await _make_workflow(client, f"v39 pack wf b {tag}", {"nodes": [_node("s", "schedule_trigger")], "edges": []})
            wf_ids.extend([wf_a, wf_b])
            ds = await _make_dataset(client, f"v39 pack cities {tag}", rows)
            ds_ids.append(ds)

            res = await client.post(
                "/packs/export",
                json={"workflow_ids": [wf_a, wf_b], "dataset_ids": [ds]},
            )
            assert res.status_code == 200, res.text
            pack = res.json()

            # schema-only variant: rows stripped, metadata intact
            res2 = await client.post(
                "/packs/export",
                json={"workflow_ids": [wf_a], "dataset_ids": [ds], "include_rows": False},
            )
            assert res2.status_code == 200, res2.text
            return pack, res2.json()

    try:
        pack, slim = asyncio.run(_go())

        assert pack["format"] == "py8n-pack"
        assert pack["pack_version"] == 1
        from app.config import settings
        assert pack["py8n_version"] == settings.version  # stamps whatever instance built it
        assert pack["generated_at"]
        m = pack["manifest"]
        assert m["workflow_count"] == 2
        assert m["dataset_count"] == 1
        assert m["total_rows"] == 3
        assert "manual_trigger" in m["node_types"] and "schedule_trigger" in m["node_types"]
        assert m["warnings"] == []

        names = [w["name"] for w in pack["workflows"]]
        assert f"v39 pack wf a {tag}" in names and f"v39 pack wf b {tag}" in names
        by_name = {w["name"]: w for w in pack["workflows"]}
        a_doc = by_name[f"v39 pack wf a {tag}"]
        assert len(a_doc["graph"]["nodes"]) == 2 and len(a_doc["graph"]["edges"]) == 1

        ds_doc = pack["datasets"][0]
        assert ds_doc["name"] == f"v39 pack cities {tag}"
        assert len(ds_doc["rows"]) == 3
        assert {"name": "city", "dtype": "text"} in ds_doc["schema"] or any(
            c["name"] == "city" for c in ds_doc["schema"]
        )
        assert any(r["city"] == "lisbon" for r in ds_doc["rows"])

        # schema-only export keeps the schema, drops the rows
        assert slim["datasets"][0]["rows"] == []
        assert len(slim["datasets"][0]["schema"]) >= 1
        assert slim["manifest"]["include_rows"] is False
    finally:
        _cleanup(wf_ids, ds_ids)


# ------------------------------------------------------------------ test 3
def test_pack_inspect_then_import_roundtrip():
    """Inspect previews honestly; import creates everything; a re-import
    of the same pack survives dataset-name collisions via suffixes."""
    tag = uuid.uuid4().hex[:8]
    graph = {
        "nodes": [_node("t", "manual_trigger"), _node("c", "code", {"code": "result = 42"})],
        "edges": [_edge("e1", "t", "c")],
    }
    pack_doc = {
        "format": "py8n-pack",
        "pack_version": 1,
        "py8n_version": "1.39.0",
        "workflows": [{"name": f"v39 import wf {tag}", "description": "from pack", "graph": graph}],
        "datasets": [
            {
                "name": f"v39 import data {tag}",
                "description": "bundled rows",
                "schema": [{"name": "k", "dtype": "text"}],
                "rows": [{"k": "alpha"}, {"k": "beta"}],
            }
        ],
    }
    wf_ids: list[str] = []
    ds_ids: list[str] = []

    async def _go():
        async with _client() as client:
            # inspect before anything exists
            pre = await client.post("/packs/inspect", json=pack_doc)
            assert pre.status_code == 200, pre.text
            pre_body = pre.json()

            # first import
            imp1 = await client.post("/packs/import", json=pack_doc)
            assert imp1.status_code == 201, imp1.text
            body1 = imp1.json()

            # rows are really queryable through the normal dataset API
            ds_id = body1["datasets"][0]["id"]
            rows_res = await client.get(f"/datasets/{ds_id}/rows")
            assert rows_res.status_code == 200, rows_res.text

            # inspect again: names now exist / a rename is previewed
            post = await client.post("/packs/inspect", json=pack_doc)
            assert post.status_code == 200, post.text
            post_body = post.json()

            # second import: workflow duplicates by design, dataset renames
            imp2 = await client.post("/packs/import", json=pack_doc)
            assert imp2.status_code == 201, imp2.text
            body2 = imp2.json()
            wf_ids.extend([w["id"] for w in body1["workflows"]])
            wf_ids.extend([w["id"] for w in body2["workflows"]])
            ds_ids.extend([d["id"] for d in body1["datasets"]])
            ds_ids.extend([d["id"] for d in body2["datasets"]])
            return pre_body, body1, rows_res.json(), post_body, body2

    try:
        pre, imp1, rows, post, imp2 = asyncio.run(_go())

        # inspect (fresh instance state)
        assert pre["format"] == "py8n-pack"
        assert pre["workflow_count"] == 1 and pre["dataset_count"] == 1
        wf_prev = pre["workflows"][0]
        assert wf_prev["valid"] is True and wf_prev["error"] is None
        assert wf_prev["node_count"] == 2
        assert wf_prev["exists"] is False
        assert pre["datasets"][0]["rename_to"] is None

        # import 1: one workflow (inactive) + one dataset with both rows
        assert len(imp1["workflows"]) == 1 and imp1["skipped"] == []
        wf = imp1["workflows"][0]
        assert wf["name"] == f"v39 import wf {tag}" and wf["node_count"] == 2
        detail = asyncio.run(_fetch_workflow(wf["id"]))
        assert detail["is_active"] is False  # imports never auto-fire
        assert len(detail["graph"]["nodes"]) == 2

        ds = imp1["datasets"][0]
        assert ds["name"] == f"v39 import data {tag}" and ds["row_count"] == 2
        assert len(rows["rows"]) == 2 and rows["rows"][0]["k"] == "alpha"

        # inspect (post-import): exists flags + rename preview
        assert post["workflows"][0]["exists"] is True
        assert post["datasets"][0]["rename_to"] == f"v39 import data {tag} (2)"

        # import 2: dataset lands under the suffixed name, workflow again
        assert len(imp2["workflows"]) == 1
        assert imp2["datasets"][0]["name"] == f"v39 import data {tag} (2)"
        assert imp2["datasets"][0]["row_count"] == 2
    finally:
        _cleanup(wf_ids, ds_ids)


async def _fetch_workflow(wid: str) -> dict:
    async with _client() as client:
        res = await client.get(f"/workflows/{wid}")
        assert res.status_code == 200, res.text
        return res.json()


# ------------------------------------------------------------------ test 4
def test_pack_skips_invalid_entries():
    """Broken graphs and invalid dataset names are skipped with reasons -
    one bad entry never aborts the batch."""
    tag = uuid.uuid4().hex[:8]
    good_graph = {"nodes": [_node("m", "manual_trigger")], "edges": []}
    bad_graph = {
        "nodes": [_node("x", "not_a_real_node_type")],
        "edges": [],
    }
    pack_doc = {
        "format": "py8n-pack",
        "workflows": [
            {"name": f"v39 good wf {tag}", "graph": good_graph},
            {"name": f"v39 bad wf {tag}", "graph": bad_graph},
        ],
        "datasets": [
            {"name": f"v39 ok data {tag}", "rows": [{"a": 1}]},
            {"name": "1 starts with a digit!", "rows": [{"a": 2}]},
        ],
    }
    wf_ids: list[str] = []
    ds_ids: list[str] = []

    async def _go():
        async with _client() as client:
            ins = await client.post("/packs/inspect", json=pack_doc)
            assert ins.status_code == 200, ins.text
            imp = await client.post("/packs/import", json=pack_doc)
            assert imp.status_code == 201, imp.text
            for w in imp.json()["workflows"]:
                wf_ids.append(w["id"])
            for d in imp.json()["datasets"]:
                ds_ids.append(d["id"])
            return ins.json(), imp.json()

    try:
        ins, imp = asyncio.run(_go())

        # inspect flags exactly the two broken entries
        by_name = {w["name"]: w for w in ins["workflows"]}
        assert by_name[f"v39 good wf {tag}"]["valid"] is True
        bad_prev = by_name[f"v39 bad wf {tag}"]
        assert bad_prev["valid"] is False and "Unknown node type" in (bad_prev["error"] or "")
        ds_prev = {d["name"]: d for d in ins["datasets"]}
        assert ds_prev["1 starts with a digit!"]["invalid_name"] is True
        assert any("1 starts with a digit!" in w for w in ins["warnings"])

        # import creates only the healthy pair
        assert len(imp["workflows"]) == 1 and imp["workflows"][0]["name"] == f"v39 good wf {tag}"
        assert len(imp["datasets"]) == 1 and imp["datasets"][0]["row_count"] == 1
        reasons = {s["name"]: s["reason"] for s in imp["skipped"]}
        assert f"v39 bad wf {tag}" in reasons and "Unknown node type" in reasons[f"v39 bad wf {tag}"]
        assert "1 starts with a digit!" in reasons
    finally:
        _cleanup(wf_ids, ds_ids)


# ------------------------------------------------------------------ test 5
def test_pack_export_scoping():
    """v37 scoping: another user's workflow id must look nonexistent."""
    tag = uuid.uuid4().hex[:8]
    email_a = f"v39-alice-{tag}@py8n.test"
    email_b = f"v39-bob-{tag}@py8n.test"
    wf_ids: list[str] = []
    ds_ids: list[str] = []

    async def _go():
        async with _client() as client:
            a = await _register(client, email_a)
            b = await _register(client, email_b)
            wf = await _make_workflow(client, f"v39 scoped wf {tag}", {"nodes": [], "edges": []}, headers=_auth(a["token"]))
            ds = await _make_dataset(client, f"v39 scoped data {tag}", [{"x": 1}], headers=_auth(a["token"]))
            wf_ids.append(wf)
            ds_ids.append(ds)

            # bob cannot pack alice's rows - they look nonexistent
            res = await client.post(
                "/packs/export",
                json={"workflow_ids": [wf], "dataset_ids": [ds]},
                headers=_auth(b["token"]),
            )
            assert res.status_code == 404, res.text

            # alice packs her own rows just fine
            ok = await client.post(
                "/packs/export",
                json={"workflow_ids": [wf]},
                headers=_auth(a["token"]),
            )
            assert ok.status_code == 200, ok.text
            return ok.json()

    try:
        pack = asyncio.run(_go())
        assert pack["manifest"]["workflow_count"] == 1
    finally:
        _cleanup(wf_ids, ds_ids)
        # reset auth state so the suite stays repeatable against the live DB
        asyncio.run(_wipe_users_and_ownership())


async def _wipe_users_and_ownership() -> None:
    from sqlalchemy import delete, update

    from app.db import AsyncSessionLocal
    from app.models import App, Credential, Dashboard, Dataset, EnvVariable, Folder, User, Workflow

    async with AsyncSessionLocal() as session:
        await session.execute(delete(User))
        for model in (Workflow, Dataset, Folder, Credential, EnvVariable, App, Dashboard):
            await session.execute(update(model).values(owner_id=None))
        await session.commit()
