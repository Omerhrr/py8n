"""V27 feature tests: Dataset Engine - first-class datasets + DuckDB SQL.

Datasets are stored as Parquet via DuckDB (metadata in SQLite): create from
JSON rows, upload xlsx/csv/json, paginate rows, profile columns, run SQL
across ALL datasets (each registered as a view named after its lowercased
name - joins included), and drive them from workflows with the three new
nodes: dataset_read / dataset_write / sql_query (Jinja-resolvable params).

Runs the FastAPI app in-process via httpx ASGITransport (same harness as v4-v26).
"""

from __future__ import annotations

import asyncio
import io
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


async def _cleanup(client_unused: None, workflow_ids: list[str], dataset_refs: list[str], env_ids: list[str]) -> None:
    async with _client() as client:
        for wid in workflow_ids:
            try:
                await client.delete(f"/workflows/{wid}")
            except Exception:
                pass
        for ref in dataset_refs:
            try:
                await client.delete(f"/datasets/{ref}")
            except Exception:
                pass
        for eid in env_ids:
            try:
                await client.delete(f"/env-vars/{eid}")
            except Exception:
                pass
    await _drain_background()


def _node(nid: str, ntype: str, params: dict | None = None, name: str | None = None) -> dict:
    return {"id": nid, "type": ntype, "name": name or nid, "position": {"x": 0, "y": 0}, "parameters": params or {}}


def _edge(eid: str, source: str, target: str) -> dict:
    return {"id": eid, "source": source, "target": target, "sourceHandle": "main", "targetHandle": "main"}


async def _make_workflow(client: httpx.AsyncClient, name: str, graph: dict) -> str:
    res = await client.post("/workflows", json={"name": name, "graph": graph, "is_active": False})
    assert res.status_code == 201, res.text
    return res.json()["id"]


async def _run_and_wait(client: httpx.AsyncClient, workflow_id: str, payload: dict | None = None) -> dict:
    res = await client.post(f"/workflows/{workflow_id}/run", json={"payload": payload or {}})
    assert res.status_code in (200, 202), res.text
    exec_id = res.json()["execution_id"]
    for _ in range(100):
        res = await client.get(f"/executions/{exec_id}")
        assert res.status_code == 200, res.text
        if res.json()["status"] != "running":
            return res.json()
        await asyncio.sleep(0.05)
    raise AssertionError("execution did not finish in time")


def _find_node_run(execution: dict, node_name: str) -> dict | None:
    for run in execution.get("node_runs") or []:
        if run.get("node_name") == node_name:
            return run
    return None


# ---------------------------------------------------------------------------
# 1) Definitions: the three dataset nodes exposed with their params
# ---------------------------------------------------------------------------
def test_v27_definitions():
    async def _go():
        async with _client() as client:
            res = await client.get("/node-definitions")
            assert res.status_code == 200
            defs = res.json()["definitions"]
            types = [d["type"] for d in defs]
            assert len(types) >= 37, f"expected 37+ visible types, got {len(types)}"  # 45 at v45
            by = {d["type"]: d for d in defs}
            for t in ("dataset_read", "dataset_write", "sql_query"):
                assert t in types, t
            props = {t: set(by[t]["parameters_schema"]["properties"].keys()) for t in by if t.startswith(("dataset", "sql"))}
            assert props["dataset_read"] == {"dataset", "limit"}
            assert props["dataset_write"] == {"dataset", "mode", "key_columns", "create_if_missing"}  # key_columns: v45 upsert
            assert props["sql_query"] == {"sql"}
            assert by["dataset_read"]["defaults"]["limit"] == 200
            assert by["dataset_write"]["parameters_schema"]["properties"]["mode"].get("options") == ["append", "replace", "upsert"]  # upsert: v45

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(None, [], [], []))


# ---------------------------------------------------------------------------
# 2) CRUD + rows pagination + profile
# ---------------------------------------------------------------------------
def test_v27_crud_rows_profile():
    tag = uuid.uuid4().hex[:8]
    name = f"v27 people {tag}"
    created: list[str] = []

    async def _go():
        rows = [
            {"name": f"p{i}", "age": 20 + i, "city": "Lagos" if i % 2 else "Berlin"}
            for i in range(25)
        ]
        async with _client() as client:
            res = await client.post("/datasets", json={"name": name, "description": "crud test", "rows": rows})
            assert res.status_code == 201, res.text
            meta = res.json()
            created.append(meta["id"])
            assert meta["row_count"] == 25 and meta["source"] == "api"
            assert {c["name"] for c in meta["schema_json"]} == {"name", "age", "city"}
            dts = {c["name"]: c["dtype"] for c in meta["schema_json"]}
            assert dts["age"] == "integer" and dts["name"] == "text"

            # fetch by NAME (resolution contract) and paginate
            res = await client.get(f"/datasets/{name}/rows", params={"offset": 20, "limit": 10})
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["row_count"] == 25 and len(body["rows"]) == 5 and body["offset"] == 20
            assert body["rows"][0]["name"] == "p20"

            # profile: numeric range + text top values
            res = await client.get(f"/datasets/{meta['id']}/profile")
            assert res.status_code == 200, res.text
            prof = res.json()
            assert prof["row_count"] == 25
            cols = {c["name"]: c for c in prof["columns"]}
            assert cols["age"]["min"] == 20 and cols["age"]["max"] == 44
            tops = {t["value"]: t["count"] for t in cols["city"]["top_values"]}
            assert tops == {"Berlin": 13, "Lagos": 12}

            # rename + duplicate guard
            res = await client.put(f"/datasets/{meta['id']}", json={"description": "renamed desc"})
            assert res.status_code == 200 and res.json()["description"] == "renamed desc"
            res = await client.post("/datasets", json={"name": name.upper(), "rows": []})
            assert res.status_code == 409

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(None, [], created, []))


# ---------------------------------------------------------------------------
# 3) Uploads: CSV + XLSX multipart, bad type 415, missing 404
# ---------------------------------------------------------------------------
def test_v27_uploads():
    tag = uuid.uuid4().hex[:8]
    created: list[str] = []

    async def _go():
        async with _client() as client:
            csv_bytes = (
                "sku,qty,price\nA1,5,9.5\nB2,3,12.0\nC3,7,4.25\n"
            ).encode()
            res = await client.post(
                "/datasets/upload",
                files={"file": ("orders.csv", csv_bytes, "text/csv")},
                data={"name": f"v27 orders {tag}", "description": "csv upload"},
            )
            assert res.status_code == 201, res.text
            order_meta = res.json()
            created.append(order_meta["id"])
            assert order_meta["row_count"] == 3 and order_meta["source"] == "upload"
            res = await client.get(f"/datasets/{order_meta['id']}/rows")
            rows = res.json()["rows"]
            assert rows[0] == {"sku": "A1", "qty": 5, "price": 9.5}  # types inferred

            # xlsx via openpyxl bytes
            from openpyxl import Workbook

            wb = Workbook()
            ws = wb.active
            ws.title = "Staff"
            ws.append(["emp", "salary"])
            ws.append(["ada", 120000])
            ws.append(["grace", 135000])
            buf = io.BytesIO()
            wb.save(buf)
            res = await client.post(
                "/datasets/upload",
                files={"file": ("staff.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                data={"name": f"v27 staff {tag}"},
            )
            assert res.status_code == 201, res.text
            staff_meta = res.json()
            created.append(staff_meta["id"])
            res = await client.get(f"/datasets/{staff_meta['id']}/rows")
            assert res.json()["rows"][1] == {"emp": "grace", "salary": 135000}

            # guards: unsupported ext / bad parse / unknown id
            res = await client.post(
                "/datasets/upload",
                files={"file": ("x.bin", b"\x00\x01", "application/octet-stream")},
                data={"name": f"v27 bad {tag}"},
            )
            assert res.status_code == 415
            res = await client.post(
                "/datasets/upload",
                files={"file": ("broken.csv", b"name\n\"unterminated", "text/csv")},
                data={"name": f"v27 broken {tag}"},
            )
            assert res.status_code in (400, 500)
            res = await client.get("/datasets/does-not-exist")
            assert res.status_code == 404

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(None, [], created, []))


# ---------------------------------------------------------------------------
# 4) SQL across datasets: WHERE/aggregate + JOIN + bad SQL 400
# ---------------------------------------------------------------------------
def test_v27_sql_query_endpoint():
    tag = uuid.uuid4().hex[:8]
    created: list[str] = []

    async def _go():
        async with _client() as client:
            a_name, b_name = f"v27 tx {tag}", f"v27 region {tag}"
            res = await client.post("/datasets", json={
                "name": a_name,
                "rows": [
                    {"id": 1, "amt": 100}, {"id": 2, "amt": 250},
                    {"id": 3, "amt": 90}, {"id": 4, "amt": 400},
                ],
            })
            assert res.status_code == 201, res.text
            created.append(res.json()["id"])
            res = await client.post("/datasets", json={
                "name": b_name,
                "rows": [{"id": 1, "region": "EU"}, {"id": 2, "region": "AF"}, {"id": 4, "region": "AF"}],
            })
            assert res.status_code == 201, res.text
            created.append(res.json()["id"])

            a_view = a_name.lower().replace(" ", "_")
            b_view = b_name.lower().replace(" ", "_")

            res = await client.post("/datasets/query", json={
                "sql": f"SELECT count(*) AS n, sum(amt) AS total FROM {a_view} WHERE amt > 95"
            })
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["rows"] == [{"n": 3, "total": 750}]
            assert body["duration_ms"] >= 0 and a_view in body["views"]

            res = await client.post("/datasets/query", json={
                "sql": (
                    f"SELECT r.region, count(*) AS n, sum(a.amt) AS total "
                    f"FROM {a_view} a JOIN {b_view} r ON a.id = r.id "
                    f"GROUP BY r.region ORDER BY total DESC"
                )
            })
            assert res.status_code == 200, res.text
            assert res.json()["rows"] == [
                {"region": "AF", "n": 2, "total": 650},
                {"region": "EU", "n": 1, "total": 100},
            ]

            res = await client.post("/datasets/query", json={"sql": "SELECT * FROM nope_missing"})
            assert res.status_code == 400
            res = await client.post("/datasets/query", json={"sql": "SELEKT broken"})
            assert res.status_code == 400

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(None, [], created, []))


# ---------------------------------------------------------------------------
# 5) dataset_read in a workflow (+ row_count via set_variable)
# ---------------------------------------------------------------------------
def test_v27_dataset_read_node():
    tag = uuid.uuid4().hex[:8]
    name = f"v27 read src {tag}"
    created: list[str] = []
    wf_ids: list[str] = []

    async def _go():
        async with _client() as client:
            res = await client.post("/datasets", json={
                "name": name,
                "rows": [
                    {"item": "desk", "amt": 120}, {"item": "chair", "amt": 80},
                    {"item": "lamp", "amt": 45},
                ],
            })
            assert res.status_code == 201, res.text
            created.append(res.json()["id"])

            graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("r", "dataset_read", {"dataset": name, "limit": 2}, "Pull"),
                    _node("s", "set_variable", {"keep_input": False, "assignments": {"n": "{{ input.row_count }}", "got": "{{ input.returned }}"}}, "Meta"),
                ],
                "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "s")],
            }
            wid = await _make_workflow(client, f"v27 read {tag}", graph)
            wf_ids.append(wid)
            run = await _run_and_wait(client, wid)
            assert run["status"] == "success", run.get("error")

            pull = _find_node_run(run, "Pull")
            assert pull["status"] == "success"
            assert [it["item"] for it in pull["output"]["items"]] == ["desk", "chair"]
            assert pull["output"]["row_count"] == 3 and pull["output"]["returned"] == 2
            assert pull["output"]["dataset"] == name

            meta = _find_node_run(run, "Meta")
            assert meta["output"] == {"n": 3, "got": 2}

            # unknown dataset -> run error
            graph2 = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("r", "dataset_read", {"dataset": f"ghost {tag}"}),
                ],
                "edges": [_edge("e1", "t", "r")],
            }
            wid2 = await _make_workflow(client, f"v27 read ghost {tag}", graph2)
            wf_ids.append(wid2)
            run2 = await _run_and_wait(client, wid2)
            assert run2["status"] == "error"
            assert "not found" in (run2.get("error") or "")

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(None, wf_ids, created, []))


# ---------------------------------------------------------------------------
# 6) dataset_write: append + create-on-missing + replace + zero/shape guards
# ---------------------------------------------------------------------------
def test_v27_dataset_write_node():
    tag = uuid.uuid4().hex[:8]
    name = f"v27 written {tag}"
    wf_ids: list[str] = []

    async def _go():
        async with _client() as client:
            # append, creating the dataset from the flow
            graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("w", "dataset_write", {"dataset": name, "mode": "append"}, "Save"),
                    _node("s", "set_variable", {"keep_input": False, "assignments": {"total": "{{ input.row_count }}", "written": "{{ input.written }}"}}, "Out"),
                ],
                "edges": [_edge("e1", "t", "w"), _edge("e2", "w", "s")],
            }
            wid = await _make_workflow(client, f"v27 write {tag}", graph)
            wf_ids.append(wid)
            run1 = await _run_and_wait(client, wid, payload={"items": [{"sku": "a", "n": 1}, {"sku": "b", "n": 2}]})
            assert run1["status"] == "success", run1.get("error")
            out1 = _find_node_run(run1, "Save")["output"]
            assert out1["created"] is True and out1["written"] == 2 and out1["row_count"] == 2
            assert _find_node_run(run1, "Out")["output"] == {"total": 2, "written": 2}

            # second append grows it
            run2 = await _run_and_wait(client, wid, payload={"items": [{"sku": "c", "n": 3}]})
            out2 = _find_node_run(run2, "Save")["output"]
            assert out2["created"] is False and out2["row_count"] == 3

            # verify persistence via API + name resolution
            res = await client.get(f"/datasets/{name}/rows")
            assert res.status_code == 200 and res.json()["row_count"] == 3

            # replace resets to exactly the new items
            graph3 = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("w", "dataset_write", {"dataset": name, "mode": "replace"}, "Save"),
                ],
                "edges": [_edge("e1", "t", "w")],
            }
            wid3 = await _make_workflow(client, f"v27 write replace {tag}", graph3)
            wf_ids.append(wid3)
            run3 = await _run_and_wait(client, wid3, payload={"items": [{"sku": "z", "n": 9}]})
            assert run3["status"] == "success", run3.get("error")
            res = await client.get(f"/datasets/{name}/rows")
            body = res.json()
            assert body["row_count"] == 1 and body["rows"][0]["sku"] == "z"

            # guards: non-dict items error; replace-with-zero error; create_if_missing off
            graph4 = {
                "nodes": [_node("t", "manual_trigger"), _node("w", "dataset_write", {"dataset": name})],
                "edges": [_edge("e1", "t", "w")],
            }
            wid4 = await _make_workflow(client, f"v27 write bad {tag}", graph4)
            wf_ids.append(wid4)
            run4 = await _run_and_wait(client, wid4, payload={"items": ["scalar", 42]})
            assert run4["status"] == "error" and "non-object" in (run4.get("error") or "")

            run5 = await _run_and_wait(client, wid3, payload={"items": []})
            assert run5["status"] == "error" and "zero items" in (run5.get("error") or "")

            graph6 = {
                "nodes": [_node("t", "manual_trigger"), _node("w", "dataset_write", {"dataset": f"v27 nomake {tag}", "create_if_missing": False})],
                "edges": [_edge("e1", "t", "w")],
            }
            wid6 = await _make_workflow(client, f"v27 write nomake {tag}", graph6)
            wf_ids.append(wid6)
            run6 = await _run_and_wait(client, wid6, payload={"items": [{"a": 1}]})
            assert run6["status"] == "error" and "not found" in (run6.get("error") or "")

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(None, wf_ids, [name], []))


# ---------------------------------------------------------------------------
# 7) sql_query node: SQL with Jinja {{ env.X }} + downstream filter
# ---------------------------------------------------------------------------
def test_v27_sql_query_node():
    tag = uuid.uuid4().hex[:8]
    name = f"v27 sales {tag}"
    created: list[str] = []
    wf_ids: list[str] = []
    env_ids: list[str] = []

    async def _go():
        async with _client() as client:
            res = await client.post("/datasets", json={
                "name": name,
                "rows": [
                    {"item": "desk", "amt": 120}, {"item": "chair", "amt": 80},
                    {"item": "lamp", "amt": 45}, {"item": "sofa", "amt": 300},
                ],
            })
            assert res.status_code == 201, res.text
            created.append(res.json()["id"])

            env_key = f"DSMIN{tag}"
            res = await client.post("/env-vars", json={"key": env_key, "value": "79"})
            assert res.status_code == 201, res.text
            env_ids.append(res.json()["id"])

            view = name.lower().replace(" ", "_")
            graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("q", "sql_query", {"sql": f"SELECT item, amt FROM {view} WHERE amt > {{{{ env.{env_key} }}}} ORDER BY amt DESC"}, "Query"),
                    _node("s", "set_variable", {"keep_input": False, "assignments": {"top": "{{ input.items[0].item }}", "n": "{{ input.row_count }}"}}, "Top"),
                ],
                "edges": [_edge("e1", "t", "q"), _edge("e2", "q", "s")],
            }
            wid = await _make_workflow(client, f"v27 sql {tag}", graph)
            wf_ids.append(wid)
            run = await _run_and_wait(client, wid)
            assert run["status"] == "success", run.get("error")

            q = _find_node_run(run, "Query")
            assert q["status"] == "success"
            assert [it["item"] for it in q["output"]["items"]] == ["sofa", "desk", "chair"]
            assert q["output"]["row_count"] == 3 and view in q["output"]["views"]
            assert _find_node_run(run, "Top")["output"] == {"top": "sofa", "n": 3}

            # empty result still flows (row_count 0)
            graph2 = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("q", "sql_query", {"sql": f"SELECT item FROM {view} WHERE amt > 9999"}, "Query"),
                    _node("s", "set_variable", {"keep_input": False, "assignments": {"n": "{{ input.row_count }}"}}, "Top"),
                ],
                "edges": [_edge("e1", "t", "q"), _edge("e2", "q", "s")],
            }
            wid2 = await _make_workflow(client, f"v27 sql empty {tag}", graph2)
            wf_ids.append(wid2)
            run2 = await _run_and_wait(client, wid2)
            assert run2["status"] == "success", run2.get("error")
            assert _find_node_run(run2, "Top")["output"] == {"n": 0}

            # broken SQL surfaces as a run error
            graph3 = {
                "nodes": [_node("t", "manual_trigger"), _node("q", "sql_query", {"sql": "DROP TABLE nope"})],
                "edges": [_edge("e1", "t", "q")],
            }
            wid3 = await _make_workflow(client, f"v27 sql bad {tag}", graph3)
            wf_ids.append(wid3)
            run3 = await _run_and_wait(client, wid3)
            assert run3["status"] == "error" and "SQL error" in (run3.get("error") or "")

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(None, wf_ids, created, env_ids))
