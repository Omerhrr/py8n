"""V50 feature tests: the Data OS foundation.

Data contracts: declarative per-dataset schema promises (dtype castability,
nullability, allowed domains) enforced at write time - the dataset_write
node and the rows API hard-stop on error-mode violations, warn-mode
violations ride along on the output; the check endpoint lints candidate
rows or the current contents.

Dataset health: freshness / volume / contract / quality derived from the
live estate (version timeline, parquet, contract) with one 0-100 score and
a healthy/degraded/unhealthy status - nothing stored, so it cannot drift.

Catalog: one derived inventory per dataset - identity, shape, freshness,
contract, PRODUCERS (version lineage) and CONSUMERS (active workflow graph
scan) - with search and tag filters.

Incremental ingestion: dataset_write mode=incremental writes only rows
beyond the stored watermark and advances the checkpoint (the CDC pattern
without source-side memory); checkpoints are per-pipeline keys, listable
and resettable.

Dataset triggers: a watched dataset's new version fires the watching
workflow exactly once (first poll arms, later polls fire on advance).

Connectors: db_source (sqlite live, SELECT-only guard) and s3_source
(fake boto3 client, csv parse, URI validation).

Runs the FastAPI app in-process via httpx ASGITransport (same harness as v4-v49).
"""

from __future__ import annotations

import asyncio
import io
import sqlite3
import uuid
from pathlib import Path

import httpx

from app.main import app
from app.services import executor as executor_mod

API = "http://testserver/api/v1"
BACKEND_DATA = Path(__file__).resolve().parents[1] / "data"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _drain_background() -> None:
    tasks = [t for t in executor_mod._background_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _node(nid: str, ntype: str, params: dict | None = None, name: str | None = None) -> dict:
    return {"id": nid, "type": ntype, "name": name or nid, "position": {"x": 0, "y": 0}, "parameters": params or {}}


def _edge(eid: str, source: str, target: str, source_handle: str = "main", target_handle: str = "main") -> dict:
    return {"id": eid, "source": source, "target": target, "sourceHandle": source_handle, "targetHandle": target_handle}


async def _make_workflow(client: httpx.AsyncClient, name: str, graph: dict, active: bool = False) -> str:
    res = await client.post("/workflows", json={"name": name, "graph": graph, "is_active": active})
    assert res.status_code == 201, res.text
    return res.json()["id"]


async def _run_and_wait(client: httpx.AsyncClient, workflow_id: str, payload: dict | None = None) -> dict:
    res = await client.post(f"/workflows/{workflow_id}/run", json={"payload": payload or {}})
    assert res.status_code in (200, 202), res.text
    exec_id = res.json()["execution_id"]
    for _ in range(200):
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


async def _mk_dataset(client: httpx.AsyncClient, name: str, rows: list[dict]) -> str:
    res = await client.post("/datasets", json={"name": name, "rows": rows})
    assert res.status_code == 201, res.text
    return res.json()["id"]


GOOD_COLS = [
    {"name": "id", "dtype": "integer", "nullable": False},
    {"name": "email", "dtype": "text"},
    {"name": "status", "dtype": "text", "allowed": ["active", "inactive"]},
]


# ---------------------------------------------------------------------------
# 1) definitions: strict pins live HERE (newest wave)
# ---------------------------------------------------------------------------
def test_v50_definitions():
    async def _go():
        async with _client() as client:
            res = await client.get("/health")
            # v51 moved the strict pin forward; v50 keeps a floor
            assert res.json()["version"] >= "1.50.0", res.json()
            res = await client.get("/node-definitions")
            defs = res.json()["definitions"]
            types = {d["type"] for d in defs}
            assert len(defs) >= 50, f"v50 adds 3 nodes; expected at least 50 types, got {len(defs)}"
            assert {"db_source", "s3_source", "dataset_trigger"} <= types, types

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


# ---------------------------------------------------------------------------
# 2) contract CRUD + validation
# ---------------------------------------------------------------------------
def test_v50_contract_crud():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            ds_id = await _mk_dataset(client, f"v50-contract-{tag}", [
                {"id": 1, "email": "a@x.com", "status": "active"},
                {"id": 2, "email": "b@x.com", "status": "inactive"},
            ])
            res = await client.put(f"/datasets/{ds_id}/contract", json={"columns": GOOD_COLS, "on_violation": "error"})
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["present"] is True
            assert body["on_violation"] == "error"
            assert body["version"] == 1
            assert len(body["columns"]) == 3

            # replacing bumps the contract version
            res = await client.put(f"/datasets/{ds_id}/contract", json={"columns": GOOD_COLS, "on_violation": "warn"})
            assert res.status_code == 200, res.text
            assert res.json()["version"] == 2
            assert res.json()["on_violation"] == "warn"

            # invalid dtype / duplicates / empty -> 400, nothing persisted
            for bad in (
                {"columns": [{"name": "x", "dtype": "float64"}]},
                {"columns": [{"name": "a"}, {"name": "a"}]},
                {"columns": []},
                {"columns": [{"name": "s", "allowed": []}]},
            ):
                res = await client.put(f"/datasets/{ds_id}/contract", json=bad)
                assert res.status_code in (400, 422), (bad, res.text)  # schema- or service-level
            res = await client.get(f"/datasets/{ds_id}/contract")
            assert res.json()["version"] == 2  # bad puts did not bump

            res = await client.delete(f"/datasets/{ds_id}/contract")
            assert res.status_code == 204
            res = await client.get(f"/datasets/{ds_id}/contract")
            assert res.json()["present"] is False

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


# ---------------------------------------------------------------------------
# 3) contract check endpoint: candidate rows or CURRENT contents
# ---------------------------------------------------------------------------
def test_v50_contract_check_endpoint():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            ds_id = await _mk_dataset(client, f"v50-check-{tag}", [
                {"id": 1, "email": "a@x.com", "status": "active"},
            ])
            cols = [dict(GOOD_COLS[0]), {**GOOD_COLS[1], "nullable": False}, GOOD_COLS[2]]
            await client.put(f"/datasets/{ds_id}/contract", json={"columns": cols})

            # castability: "7" IS an integer; "abc" is not
            res = await client.post(f"/datasets/{ds_id}/contract/check", json={
                "rows": [{"id": "7", "email": "x@y.z", "status": "active"}],
            })
            assert res.status_code == 200, res.text
            assert res.json()["ok"] is True, res.json()

            res = await client.post(f"/datasets/{ds_id}/contract/check", json={
                "rows": [
                    {"id": "abc", "email": "x@y.z", "status": "active"},
                    {"id": 3, "email": None, "status": "banned"},
                    {"id": 4, "status": "active"},  # email absent = null
                ],
            })
            body = res.json()
            assert body["ok"] is False
            rules = {v["column"]: v["rule"] for v in body["violations"]}
            assert rules.get("id") == "dtype"
            assert rules.get("status") == "allowed"
            assert rules.get("email") == "not_null"
            assert body["checked_rows"] == 3

            # no rows -> lints the CURRENT dataset contents (clean -> ok)
            res = await client.post(f"/datasets/{ds_id}/contract/check", json={"rows": []})
            assert res.status_code == 200, res.text
            assert res.json()["ok"] is True, res.json()

            # no contract at all -> 400
            other = await _mk_dataset(client, f"v50-check-bare-{tag}", [{"a": 1}])
            res = await client.post(f"/datasets/{other}/contract/check", json={"rows": []})
            assert res.status_code == 400

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


# ---------------------------------------------------------------------------
# 4) rows API enforcement: error mode 422 + nothing landed; warn proceeds
# ---------------------------------------------------------------------------
def test_v50_contract_enforced_on_rows_api():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            ds_id = await _mk_dataset(client, f"v50-api-gate-{tag}", [
                {"id": 1, "email": "a@x.com", "status": "active"},
            ])
            await client.put(f"/datasets/{ds_id}/contract", json={"columns": GOOD_COLS, "on_violation": "error"})

            res = await client.post(f"/datasets/{ds_id}/rows", json={
                "rows": [{"id": 2, "email": "b@x.com", "status": "BANNED"}],
            })
            assert res.status_code == 422, res.text
            assert "contract" in res.json()["detail"].lower()
            res = await client.get(f"/datasets/{ds_id}/rows")
            assert res.json()["row_count"] == 1, "denied rows must not land"

            res = await client.post(f"/datasets/{ds_id}/rows", json={
                "rows": [{"id": "2", "email": "b@x.com", "status": "active"}],
            })
            assert res.status_code == 200, res.text  # castable strings pass

            # warn mode: violations ride along, write proceeds
            await client.put(f"/datasets/{ds_id}/contract", json={"columns": GOOD_COLS, "on_violation": "warn"})
            res = await client.post(f"/datasets/{ds_id}/rows", json={
                "rows": [{"id": 3, "email": "c@x.com", "status": "bogus"}],
            })
            assert res.status_code == 200, res.text
            res = await client.get(f"/datasets/{ds_id}/rows")
            assert res.json()["row_count"] == 3

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


# ---------------------------------------------------------------------------
# 5) contract enforced through the ENGINE (dataset_write node)
# ---------------------------------------------------------------------------
def test_v50_contract_enforced_in_workflow_write():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            ds_id = await _mk_dataset(client, f"v50-gate-{tag}", [
                {"id": 1, "email": "a@x.com", "status": "active"},
            ])
            await client.put(f"/datasets/{ds_id}/contract", json={"columns": GOOD_COLS, "on_violation": "error"})
            graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("w", "dataset_write", {"dataset": f"v50-gate-{tag}", "mode": "append"}),
                ],
                "edges": [_edge("e1", "t", "w")],
            }
            wf = await _make_workflow(client, f"v50-gate-run-{tag}", graph)

            bad = await _run_and_wait(client, wf, {"items": [
                {"id": 2, "email": "b@x.com", "status": "bogus"},
            ]})
            assert bad["status"] == "error", bad.get("error")
            run = _find_node_run(bad, "w")
            assert "contract" in (run.get("error") or "").lower() or "data contract" in str(bad.get("error") or "").lower()
            res = await client.get(f"/datasets/{ds_id}/rows")
            assert res.json()["row_count"] == 1, "error-mode violation must not land"

            good = await _run_and_wait(client, wf, {"items": [
                {"id": "2", "email": "b@x.com", "status": "active"},
            ]})
            assert good["status"] == "success", good.get("error")

            # warn mode: the write succeeds and the violations report rides along
            await client.put(f"/datasets/{ds_id}/contract", json={"columns": GOOD_COLS, "on_violation": "warn"})
            warned = await _run_and_wait(client, wf, {"items": [
                {"id": 3, "email": "c@x.com", "status": "bogus"},
            ]})
            assert warned["status"] == "success", warned.get("error")
            out = _find_node_run(warned, "w")["output"]
            assert out["contract"]["ok"] is False
            assert out["contract"]["on_violation"] == "warn"
            assert out["contract"]["violations"][0]["column"] == "status"

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


# ---------------------------------------------------------------------------
# 6) dataset health: derived freshness / volume / contract / quality
# ---------------------------------------------------------------------------
def test_v50_health_endpoint():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            ds_id = await _mk_dataset(client, f"v50-health-{tag}", [
                {"id": 1, "email": "a@x.com", "status": "active"},
                {"id": 2, "email": "b@x.com", "status": "inactive"},
            ])
            res = await client.get(f"/datasets/{ds_id}/health")
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["dataset_id"] == ds_id
            assert body["status"] in ("healthy", "degraded", "unhealthy")
            assert body["freshness"]["tier"] == "fresh"
            assert body["volume"]["rows"] == 2
            assert body["volume"]["previous_rows"] is None  # only one version so far
            assert body["schema"]["contract_present"] is False
            assert 0 <= body["score"] <= 100

            # second write: volume delta becomes visible
            await client.post(f"/datasets/{ds_id}/rows", json={
                "rows": [{"id": 3, "email": "c@x.com", "status": "active"}],
            })
            body = (await client.get(f"/datasets/{ds_id}/health")).json()
            assert body["volume"]["rows"] == 3
            assert body["volume"]["previous_rows"] == 2
            assert body["volume"]["delta"] == 1
            assert body["volume"]["delta_pct"] == 50.0

            # warn-contract violated by the live data: schema signal flips,
            # the score is pulled down but stays bounded
            cols = [{"name": "id", "dtype": "integer"}, {"name": "email", "dtype": "text"},
                    {"name": "status", "dtype": "text", "allowed": ["active", "inactive"]}]
            await client.put(f"/datasets/{ds_id}/contract", json={"columns": cols, "on_violation": "warn"})
            res = await client.post(f"/datasets/{ds_id}/rows", json={
                "rows": [{"id": 4, "email": "d@x.com", "status": "bogus"}],  # warn: lands anyway
            })
            assert res.status_code == 200, res.text
            body = (await client.get(f"/datasets/{ds_id}/health")).json()
            assert body["schema"]["contract_present"] is True
            assert body["schema"]["contract_ok"] is False
            assert body["schema"]["contract_violations"][0]["column"] == "status"
            assert body["status"] == "degraded", body["score"]

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


# ---------------------------------------------------------------------------
# 7) catalog: identity, freshness, contract, producers, consumers
# ---------------------------------------------------------------------------
def test_v50_catalog():
    tag = uuid.uuid4().hex[:8]
    ds_name = f"v50-cat-{tag}"

    async def _go():
        async with _client() as client:
            ds_id = await _mk_dataset(client, ds_name, [
                {"id": 1, "email": "a@x.com", "status": "active"},
            ])
            await client.put(f"/datasets/{ds_id}/contract", json={"columns": GOOD_COLS})
            # a consumer: an ACTIVE workflow whose node references the dataset
            consumer_graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("r", "dataset_read", {"dataset": ds_name}),
                ],
                "edges": [_edge("e1", "t", "r")],
            }
            wf_consumer = await _make_workflow(client, f"v50-reader-{tag}", consumer_graph, active=True)
            # a producer: run a workflow that writes the dataset (lineage stamps it)
            producer_graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("w", "dataset_write", {"dataset": ds_name, "mode": "append"}),
                ],
                "edges": [_edge("e1", "t", "w")],
            }
            wf_producer = await _make_workflow(client, f"v50-writer-{tag}", producer_graph)
            run = await _run_and_wait(client, wf_producer, {"items": [{"id": 2, "email": "b@x.com", "status": "inactive"}]})
            assert run["status"] == "success", run.get("error")

            res = await client.get("/catalog")
            assert res.status_code == 200, res.text
            entries = {e["name"]: e for e in res.json()["entries"]}
            entry = entries[ds_name]
            assert entry["id"] == ds_id
            assert entry["rows"] == 2
            assert entry["columns"] == 3
            assert entry["freshness"]["tier"] == "fresh"
            assert entry["contract"]["present"] is True
            assert entry["contract"]["on_violation"] == "warn"
            assert f"v50-writer-{tag}" in entry["producers"], entry
            assert f"v50-reader-{tag}" in entry["consumers"], entry

            # search + tag filters
            res = await client.get(f"/catalog?q={ds_name}")
            assert res.json()["count"] >= 1
            res = await client.get("/catalog?q=zzz-not-a-thing-zzz")
            assert res.json()["count"] == 0
            res = await client.get(f"/catalog?tag=nope")
            assert all("nope" not in e["tags"] for e in res.json()["entries"])

            # foreign-owner visibility is covered by the scoping suite; here the
            # anonymous caller just gets a well-shaped payload
            res = await client.get("/catalog?q=")
            assert "entries" in res.json()

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


# ---------------------------------------------------------------------------
# 8) incremental ingestion: checkpoint advances, replay is idempotent
# ---------------------------------------------------------------------------
def test_v50_incremental_write():
    tag = uuid.uuid4().hex[:8]
    ds_name = f"v50-incr-{tag}"

    async def _go():
        async with _client() as client:
            graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("w", "dataset_write", {
                        "dataset": ds_name,
                        "mode": "incremental",
                        "watermark_column": "ts",
                        "ingestion_key": "pipe1",
                    }),
                ],
                "edges": [_edge("e1", "t", "w")],
            }
            wf = await _make_workflow(client, f"v50-incr-run-{tag}", graph)

            # run 1: checkpoint is empty -> everything with a watermark lands
            r1 = await _run_and_wait(client, wf, {"items": [
                {"ts": "1", "v": "a"}, {"ts": "3", "v": "b"}, {"v": "no-ts"},
            ]})
            assert r1["status"] == "success", r1.get("error")
            out1 = _find_node_run(r1, "w")["output"]
            assert out1["written"] == 2, out1
            assert out1["skipped"] == 1
            assert out1["checkpoint_before"] is None
            assert out1["checkpoint_after"] == "3"

            # run 2: only rows strictly beyond 3 land (replay is idempotent)
            r2 = await _run_and_wait(client, wf, {"items": [
                {"ts": "2", "v": "old"}, {"ts": "3", "v": "same"}, {"ts": "10", "v": "new"},
            ]})
            out2 = _find_node_run(r2, "w")["output"]
            assert out2["written"] == 1, out2
            assert out2["skipped"] == 2
            assert out2["checkpoint_before"] == "3"
            assert out2["checkpoint_after"] == "10"

            ds_id = (await client.get("/datasets")).json()
            ds_id = next(d["id"] for d in ds_id if d["name"] == ds_name)
            res = await client.get(f"/datasets/{ds_id}/rows")
            assert res.json()["row_count"] == 3  # a, b, new
            assert res.json()["rows"][-1]["v"] == "new"

            # checkpoint inventory + stats
            res = await client.get(f"/datasets/{ds_id}/ingestion-states")
            states = {s["key"]: s for s in res.json()}
            assert states["pipe1"]["watermark"] == "10"
            assert states["pipe1"]["runs"] == 2
            assert states["pipe1"]["rows_total"] == 3

            # reset -> next run re-ingests from scratch
            res = await client.delete(f"/datasets/{ds_id}/ingestion-states/pipe1")
            assert res.status_code == 204
            r3 = await _run_and_wait(client, wf, {"items": [{"ts": "10", "v": "again"}]})
            out3 = _find_node_run(r3, "w")["output"]
            assert out3["written"] == 1
            assert out3["checkpoint_before"] is None

            # incremental without watermark_column is a clean node error
            bad_graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("w", "dataset_write", {"dataset": ds_name, "mode": "incremental"}),
                ],
                "edges": [_edge("e1", "t", "w")],
            }
            bad_wf = await _make_workflow(client, f"v50-incr-bad-{tag}", bad_graph)
            bad = await _run_and_wait(client, bad_wf, {"items": [{"ts": "1"}]})
            assert bad["status"] == "error"

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


# ---------------------------------------------------------------------------
# 9) dataset trigger: arm on first poll, fire exactly once per new version
# ---------------------------------------------------------------------------
def test_v50_dataset_trigger_poll():
    tag = uuid.uuid4().hex[:8]
    ds_name = f"v50-trig-{tag}"

    async def _go():
        from app.services.scheduler import _poll_dataset_trigger

        async with _client() as client:
            ds_id = await _mk_dataset(client, ds_name, [{"id": 1, "v": "a"}])
            graph = {
                "nodes": [
                    _node("trig", "dataset_trigger", {"dataset": ds_name, "poll_seconds": 30}),
                    _node("w", "dataset_write", {"dataset": f"v50-trig-out-{tag}", "mode": "append"}),
                ],
                "edges": [_edge("e1", "trig", "w")],
            }
            wf_id = await _make_workflow(client, f"v50-trigger-wf-{tag}", graph, active=True)

            async def _exec_count() -> int:
                res = await client.get(f"/executions?workflow_id={wf_id}&limit=100")
                assert res.status_code == 200, res.text
                return len(res.json())

            # first poll ARMS the watcher (records the current version, no run)
            await _poll_dataset_trigger(wf_id, "trig")
            assert await _exec_count() == 0

            # no new version -> still nothing
            await _poll_dataset_trigger(wf_id, "trig")
            assert await _exec_count() == 0

            # a new version lands -> exactly one run fires, and it succeeds
            res = await client.post(f"/datasets/{ds_id}/rows", json={"rows": [{"id": 2, "v": "b"}]})
            assert res.status_code == 200, res.text
            await _poll_dataset_trigger(wf_id, "trig")
            await _drain_background()
            assert await _exec_count() == 1
            res = await client.get(f"/executions?workflow_id={wf_id}&limit=100")
            exec_row = res.json()[0]
            assert exec_row["status"] == "success", exec_row
            assert exec_row.get("trigger_type") == "dataset", exec_row

            # the SAME version never double-fires
            await _poll_dataset_trigger(wf_id, "trig")
            await _drain_background()
            assert await _exec_count() == 1

            # the trigger node rendered the dataset meta into the flow
            res = await client.get(f"/executions/{exec_row['id']}")
            run = _find_node_run(res.json(), "trig")
            assert run["output"]["dataset"] == ds_name
            assert run["output"]["version"] == 2
            assert run["output"]["row_count"] == 2

            # the downstream node ran with the payload (out dataset got rows)
            res = await client.get("/datasets")
            out_ds = next(d for d in res.json() if d["name"] == f"v50-trig-out-{tag}")
            assert out_ds["row_count"] == 1

            # another version -> fires again (the data-DAG chain advances)
            await client.post(f"/datasets/{ds_id}/rows", json={"rows": [{"id": 3, "v": "c"}]})
            await _poll_dataset_trigger(wf_id, "trig")
            await _drain_background()
            assert await _exec_count() == 2

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


# ---------------------------------------------------------------------------
# 10) db_source: live sqlite read + SELECT-only guard
# ---------------------------------------------------------------------------
def test_v50_db_source_sqlite():
    tag = uuid.uuid4().hex[:8]
    db_path = BACKEND_DATA / f"v50_conn_{tag}.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, ltv REAL)")
    conn.execute("INSERT INTO customers (name, ltv) VALUES ('alice', 120.5), ('bob', 80.0)")
    conn.commit()
    conn.close()

    async def _go():
        async with _client() as client:
            graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("db", "db_source", {
                        "backend": "sqlite",
                        "connection": str(db_path),
                        "table": "customers",
                    }),
                ],
                "edges": [_edge("e1", "t", "db")],
            }
            wf = await _make_workflow(client, f"v50-db-run-{tag}", graph)
            run = await _run_and_wait(client, wf)
            assert run["status"] == "success", run.get("error")
            out = _find_node_run(run, "db")["output"]
            assert out["row_count"] == 2
            assert out["backend"] == "sqlite"
            names = sorted(r["name"] for r in out["items"])
            assert names == ["alice", "bob"]

            # SQL path works too
            sql_graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("db", "db_source", {
                        "backend": "sqlite",
                        "connection": str(db_path),
                        "sql": "SELECT name FROM customers WHERE ltv > 100",
                    }),
                ],
                "edges": [_edge("e1", "t", "db")],
            }
            wf2 = await _make_workflow(client, f"v50-db-sql-{tag}", sql_graph)
            run2 = await _run_and_wait(client, wf2)
            out2 = _find_node_run(run2, "db")["output"]
            assert out2["row_count"] == 1 and out2["items"][0]["name"] == "alice"

            # the read-only guard: writes are refused at the node level
            bad_graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("db", "db_source", {
                        "backend": "sqlite",
                        "connection": str(db_path),
                        "sql": "DELETE FROM customers",
                    }),
                ],
                "edges": [_edge("e1", "t", "db")],
            }
            wf3 = await _make_workflow(client, f"v50-db-bad-{tag}", bad_graph)
            run3 = await _run_and_wait(client, wf3)
            assert run3["status"] == "error"
            err3 = str(_find_node_run(run3, "db").get("error") or "").lower()
            assert "only select" in err3, err3  # the read-only guard refused DELETE

            # a missing table surfaces as a clean node error, not a crash
            miss_graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("db", "db_source", {
                        "backend": "sqlite",
                        "connection": str(db_path),
                        "table": "nope",
                    }),
                ],
                "edges": [_edge("e1", "t", "db")],
            }
            wf4 = await _make_workflow(client, f"v50-db-miss-{tag}", miss_graph)
            run4 = await _run_and_wait(client, wf4)
            assert run4["status"] == "error"

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())
        if db_path.exists():
            db_path.unlink()


# ---------------------------------------------------------------------------
# 11) s3_source: URI validation + parse (fake boto3 client, no network)
# ---------------------------------------------------------------------------
def test_v50_s3_source():
    tag = uuid.uuid4().hex[:8]

    class FakeS3:
        def __init__(self, body: bytes):
            self.body = body
            self.calls = []

        def get_object(self, Bucket, Key):
            self.calls.append((Bucket, Key))
            return {"Body": io.BytesIO(self.body)}

    csv_body = b"name,ltv\nalice,120.5\nbob,80.0\n"
    fake = FakeS3(csv_body)
    import boto3

    real_client = boto3.client
    boto3.client = lambda *a, **k: fake

    async def _go():
        async with _client() as client:
            graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("s3", "s3_source", {"uri": "s3://py8n-demo/customers.csv"}),
                ],
                "edges": [_edge("e1", "t", "s3")],
            }
            wf = await _make_workflow(client, f"v50-s3-run-{tag}", graph)
            run = await _run_and_wait(client, wf)
            assert run["status"] == "success", run.get("error")
            out = _find_node_run(run, "s3")["output"]
            assert out["row_count"] == 2
            assert out["bucket"] == "py8n-demo"
            assert out["key"] == "customers.csv"
            assert fake.calls == [("py8n-demo", "customers.csv")]

            # bad URI / unknown extension / parquet-less bucket all clean errors
            for uri in ("http://x/y.csv", "s3://only-bucket", "s3://b/k.txt"):
                g = {
                    "nodes": [
                        _node("t", "manual_trigger"),
                        _node("s3", "s3_source", {"uri": uri}),
                    ],
                    "edges": [_edge("e1", "t", "s3")],
                }
                w = await _make_workflow(client, f"v50-s3-bad-{tag}-{abs(hash(uri)) % 9999}", g)
                r = await _run_and_wait(client, w)
                assert r["status"] == "error", uri

    try:
        asyncio.run(_go())
    finally:
        boto3.client = real_client
        asyncio.run(_drain_background())
