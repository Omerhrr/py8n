"""V47 feature tests: deep data estate III - production operations.

Drift monitoring: model_train captures per-feature reference distributions
(numeric quantile bins / categorical counts) on the registry row, the new
drift_check node PSI-scores a batch against them (warn = annotate, error =
fail the run), and GET /models/{ref}/drift scores a whole dataset.
Share tokens: apps + dashboards grow an owner-controlled share-token ACL on
their public runtime surfaces (?t= or X-Share-Token; NULL = legacy open).
Cross-filtering: dashboards runtime accepts ?filter.COL= and re-computes
every component over the filtered frames. Lineage: engine dataset writes
stamp the version timeline with workflow/execution/node provenance, exposed
at GET /datasets/{id}/lineage. Templates: scheduled retraining + nightly ETL
with a quality gate.

Runs the FastAPI app in-process via httpx ASGITransport (same harness as v4-v46).
"""

from __future__ import annotations

import asyncio
import json
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


async def _cleanup(workflow_ids: list[str], dataset_refs: list[str], app_refs: list[str], dashboard_ids: list[str], model_ids: list[str]) -> None:
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
        for ref in app_refs:
            try:
                await client.delete(f"/apps/{ref}")
            except Exception:
                pass
        for did in dashboard_ids:
            try:
                await client.delete(f"/dashboards/{did}")
            except Exception:
                pass
        for mid in model_ids:
            try:
                await client.delete(f"/models/{mid}")
            except Exception:
                pass
    await _drain_background()


def _node(nid: str, ntype: str, params: dict | None = None, name: str | None = None) -> dict:
    return {"id": nid, "type": ntype, "name": name or nid, "position": {"x": 0, "y": 0}, "parameters": params or {}}


def _edge(eid: str, source: str, target: str, source_handle: str = "main", target_handle: str = "main") -> dict:
    return {"id": eid, "source": source, "target": target, "sourceHandle": source_handle, "targetHandle": target_handle}


async def _make_workflow(client: httpx.AsyncClient, name: str, graph: dict) -> str:
    res = await client.post("/workflows", json={"name": name, "graph": graph, "is_active": False})
    assert res.status_code == 201, res.text
    return res.json()["id"]


async def _run_and_wait(client: httpx.AsyncClient, workflow_id: str, payload: dict | None = None) -> dict:
    res = await client.post(f"/workflows/{workflow_id}/run", json={"payload": payload or {}})
    assert res.status_code in (200, 202), res.text
    exec_id = res.json()["execution_id"]
    for _ in range(300):
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


def _clf_rows(n: int = 80, shift: float = 0.0, seed: int = 7) -> list[dict]:
    import random

    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        group = rng.choice(["A", "B"])
        rows.append({
            "x1": round(rng.gauss(2 + shift if group == "A" else -2 + shift, 1.0), 3),
            "x2": round(rng.gauss(0, 1.0), 3),
            "city": rng.choice(["lagos", "cairo", "nairobi"]),
            "tier": group,
        })
    return rows


# ---------------------------------------------------------------------------
# 1) Definitions: 47 types, drift_check exposed, version pinned, templates ship
# ---------------------------------------------------------------------------
def test_v47_definitions():
    async def _go():
        async with _client() as client:
            res = await client.get("/health")
            assert res.json()["version"] == "1.47.0"
            res = await client.get("/node-definitions")
            defs = res.json()["definitions"]
            types = [d["type"] for d in defs]
            assert len(types) == 47, f"expected 47 visible types at v47, got {len(types)}"
            by = {d["type"]: d for d in defs}
            assert "drift_check" in types
            props = set(by["drift_check"]["parameters_schema"]["properties"].keys())
            assert props == {"model", "threshold", "on_drift"}

            # templates: the two v47 production templates ship and validate
            res = await client.get("/templates")
            templates = res.json()
            ids = [t["id"] for t in templates]
            assert "scheduled-retraining" in ids and "nightly-etl-quality-gate" in ids
            retrek = next(t for t in templates if t["id"] == "scheduled-retraining")
            assert "drift_check" in retrek["node_types"] and "model_train" in retrek["node_types"]
            etl = next(t for t in templates if t["id"] == "nightly-etl-quality-gate")
            for nt in ("cast_columns", "handle_nulls", "data_quality", "dataset_write", "dataset_export"):
                assert nt in etl["node_types"], nt
            res = await client.get("/templates/scheduled-retraining")
            assert res.status_code == 200 and "graph" in res.json()
            res = await client.get("/templates/nightly-etl-quality-gate")
            assert res.status_code == 200 and "graph" in res.json()

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup([], [], [], [], []))


# ---------------------------------------------------------------------------
# 2) Drift engine unit behavior (PSI math, missing features, serializability)
# ---------------------------------------------------------------------------
def test_v47_drift_engine_unit():
    import pandas as pd

    from app.services.models import compute_reference_stats, score_drift

    ref_df = pd.DataFrame({
        "age": list(range(20, 70)) * 2,
        "plan": ["basic", "pro", "pro", "enterprise"] * 25,
        "flag": [1] * 100,
    })
    feats = ["age", "plan", "flag"]
    stats = compute_reference_stats(ref_df, feats)
    assert stats["age"]["type"] == "numeric" and len(stats["age"]["edges"]) >= 2
    assert stats["plan"]["type"] == "categorical" and stats["plan"]["counts"]["pro"] == 50
    assert "constant" in stats["flag"]  # constant column captured as constant
    json.dumps(stats)  # JSON-serializable by construction

    # identical distribution -> ~zero PSI, no drift
    same = score_drift(stats, ref_df, feats)
    assert same["drift_detected"] is False
    assert same["overall_psi"] is not None and same["overall_psi"] < 0.05

    # shifted distribution -> drift on every shifted surface
    shift_df = pd.DataFrame({
        "age": list(range(60, 110)) * 2,
        "plan": ["free", "free", "trial", "enterprise"] * 25,
        "flag": [2] * 100,
    })
    shifted = score_drift(stats, shift_df, feats)
    assert shifted["drift_detected"] is True and shifted["overall_psi"] > 0.25
    statuses = {f["feature"]: f["status"] for f in shifted["features"]}
    assert statuses["age"] == "drifted" and statuses["flag"] == "drifted"
    assert statuses["plan"] in ("moderate", "drifted")

    # missing feature column -> missing status + drift flag (safety)
    missing = score_drift(stats, ref_df.drop(columns=["age"]), feats)
    age_row = next(f for f in missing["features"] if f["feature"] == "age")
    assert age_row["status"] == "missing" and missing["drift_detected"] is True

    # untrained feature (not in stats) is skipped silently
    partial = score_drift(stats, ref_df, ["age", "ghost"])
    assert [f["feature"] for f in partial["features"]] == ["age"]


# ---------------------------------------------------------------------------
# 3) Registry captures reference stats; drift_check node gates a run
# ---------------------------------------------------------------------------
def test_v47_drift_node_and_registry():
    tag = uuid.uuid4().hex[:8]
    model_name = f"drift-{tag}"
    wf_ids, ds_refs, model_ids = [], [], []

    async def _go():
        async with _client() as client:
            ds_id = await _mk_dataset(client, f"v47-train-{tag}", _clf_rows(80))
            ds_refs.append(f"v47-train-{tag}")
            ds_shift = await _mk_dataset(client, f"v47-shift-{tag}", _clf_rows(60, shift=8.0))
            ds_refs.append(f"v47-shift-{tag}")

            # read → train → read-back → drift: the gate scores the STORED
            # training table (the production pattern - model_train's output
            # payload is the prediction sample, not the feature rows)
            graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("d", "dataset_read", {"dataset": f"v47-train-{tag}", "limit": 80}),
                    _node("tr", "model_train", {
                        "model": "random_forest_classifier", "task": "classification",
                        "target": "tier", "model_name": model_name, "register": True,
                    }),
                    _node("d2", "dataset_read", {"dataset": f"v47-train-{tag}", "limit": 80}),
                    _node("dc", "drift_check", {"model": model_name, "on_drift": "warn"}),
                ],
                "edges": [
                    _edge("e0", "t", "d"), _edge("e1", "d", "tr"),
                    _edge("e2", "tr", "d2"), _edge("e3", "d2", "dc"),
                ],
            }
            wf = await _make_workflow(client, f"v47-drift-train-{tag}", graph)
            wf_ids.append(wf)
            exec_row = await _run_and_wait(client, wf)
            assert exec_row["status"] == "success", exec_row.get("error")

            # registry row carries reference stats
            res = await client.get(f"/models/{model_name}")
            assert res.status_code == 200, res.text
            model_row = res.json()
            model_ids.append(model_row["id"])
            assert model_row["has_reference_stats"] is True
            stats = model_row["reference_stats"]
            assert "_meta" in stats and "x1" in stats and "city" in stats
            assert stats["x1"]["type"] == "numeric" and stats["city"]["type"] == "categorical"

            # drift_check on the SAME data: no drift, report annotated, items pass through
            run_out = _find_node_run(exec_row, "dc")["output"]
            assert run_out["drift_detected"] is False
            assert run_out["report"]["model"]["name"] == model_name
            assert len(run_out["items"]) == 80  # pass-through preserved

            # warn mode + shifted data: succeeds but flags drift
            graph_warn = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("d", "dataset_read", {"dataset": f"v47-shift-{tag}", "limit": 60}),
                    _node("dc", "drift_check", {"model": model_name, "on_drift": "warn"}),
                ],
                "edges": [_edge("e0", "t", "d"), _edge("e1", "d", "dc")],
            }
            wf_warn = await _make_workflow(client, f"v47-drift-warn-{tag}", graph_warn)
            wf_ids.append(wf_warn)
            exec_warn = await _run_and_wait(client, wf_warn)
            assert exec_warn["status"] == "success", exec_warn.get("error")
            warn_out = _find_node_run(exec_warn, "dc")["output"]
            assert warn_out["drift_detected"] is True
            assert warn_out["report"]["overall_psi"] > 0.25

            # error mode + shifted data: the run FAILS with a clean message
            graph_err = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("d", "dataset_read", {"dataset": f"v47-shift-{tag}", "limit": 60}),
                    _node("dc", "drift_check", {"model": model_name, "on_drift": "error"}),
                ],
                "edges": [_edge("e0", "t", "d"), _edge("e1", "d", "dc")],
            }
            wf_err = await _make_workflow(client, f"v47-drift-err-{tag}", graph_err)
            wf_ids.append(wf_err)
            exec_err = await _run_and_wait(client, wf_err)
            assert exec_err["status"] == "error"
            assert "Drift detected" in (exec_err.get("error") or "")

            # no-reference-stats guard: model_predict's legacy normalizer path
            # does not apply here - drift_check needs stats and says so cleanly
            graph_ghost = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("d", "dataset_read", {"dataset": f"v47-train-{tag}", "limit": 10}),
                    _node("dc", "drift_check", {"model": "ghost-model-nope"}),
                ],
                "edges": [_edge("e0", "t", "d"), _edge("e1", "d", "dc")],
            }
            wf_ghost = await _make_workflow(client, f"v47-drift-ghost-{tag}", graph_ghost)
            wf_ids.append(wf_ghost)
            exec_ghost = await _run_and_wait(client, wf_ghost)
            assert exec_ghost["status"] == "error"
            assert "not found in the registry" in (exec_ghost.get("error") or "")

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids, ds_refs, [], [], model_ids))


# ---------------------------------------------------------------------------
# 4) Drift endpoint: score a whole dataset against the active version
# ---------------------------------------------------------------------------
def test_v47_drift_endpoint():
    tag = uuid.uuid4().hex[:8]
    model_name = f"drift-api-{tag}"
    wf_ids, ds_refs, model_ids = [], [], []

    async def _go():
        async with _client() as client:
            ds_id = await _mk_dataset(client, f"v47-api-train-{tag}", _clf_rows(80))
            ds_refs.append(f"v47-api-train-{tag}")
            ds_shift = await _mk_dataset(client, f"v47-api-shift-{tag}", _clf_rows(60, shift=8.0))
            ds_refs.append(f"v47-api-shift-{tag}")

            graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("d", "dataset_read", {"dataset": f"v47-api-train-{tag}", "limit": 80}),
                    _node("tr", "model_train", {
                        "model": "random_forest_classifier", "task": "classification",
                        "target": "tier", "model_name": model_name, "register": True,
                    }),
                ],
                "edges": [_edge("e0", "t", "d"), _edge("e1", "d", "tr")],
            }
            wf = await _make_workflow(client, f"v47-api-train-{tag}", graph)
            wf_ids.append(wf)
            exec_row = await _run_and_wait(client, wf)
            assert exec_row["status"] == "success", exec_row.get("error")

            # name -> ACTIVE version; stable dataset scores clean
            res = await client.get(f"/models/{model_name}/drift", params={"dataset_id": ds_id})
            assert res.status_code == 200, res.text
            report = res.json()
            assert report["drift_detected"] is False
            assert report["model"]["name"] == model_name
            assert report["dataset"]["id"] == ds_id
            assert report["dataset"]["rows"] == 80

            # shifted dataset drifts
            res = await client.get(f"/models/{model_name}/drift", params={"dataset_id": ds_shift})
            assert res.status_code == 200, res.text
            assert res.json()["drift_detected"] is True

            # custom threshold is honored
            res = await client.get(
                f"/models/{model_name}/drift", params={"dataset_id": ds_shift, "threshold": 50.0}
            )
            assert res.status_code == 200 and res.json()["drift_detected"] is False

            # unknown model / unknown dataset -> 404
            res = await client.get("/models/ghost/drift", params={"dataset_id": ds_id})
            assert res.status_code == 404
            res = await client.get(f"/models/{model_name}/drift", params={"dataset_id": "nope"})
            assert res.status_code == 404

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids, ds_refs, [], [], model_ids))


# ---------------------------------------------------------------------------
# 5) Share tokens: apps + dashboards runtime ACL
# ---------------------------------------------------------------------------
def test_v47_share_tokens():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            ds_id = await _mk_dataset(client, f"v47-share-{tag}", [
                {"name": f"c{i}", "mrr": 10 + i} for i in range(1, 11)
            ])

            # ---- app surface
            res = await client.post("/apps", json={
                "name": f"v47-share-app-{tag}", "dataset_id": ds_id,
                "config": {"components": [
                    {"id": "t1", "type": "table", "title": "R", "columns": ["name", "mrr"]},
                    {"id": "f1", "type": "form", "title": "Add", "fields": [
                        {"name": "name", "label": "Name", "type": "text", "required": True},
                        {"name": "mrr", "label": "MRR", "type": "number", "required": False},
                    ]},
                ]},
            })
            assert res.status_code == 201, res.text
            app_row = res.json()
            app_id, app_slug = app_row["id"], app_row["slug"]
            await client.post(f"/apps/{app_id}/publish")

            # legacy open access before a token exists
            res = await client.get(f"/apps/{app_slug}/runtime")
            assert res.status_code == 200

            # enable share protection
            res = await client.put(f"/apps/{app_id}/share", json={"enabled": True})
            assert res.status_code == 200, res.text
            token = res.json()["share_token"]
            assert token and len(token) >= 20

            # no token / wrong token -> 403; query + header + param -> 200
            res = await client.get(f"/apps/{app_slug}/runtime")
            assert res.status_code == 403
            res = await client.get(f"/apps/{app_slug}/runtime", params={"t": "wrong"})
            assert res.status_code == 403
            res = await client.get(f"/apps/{app_slug}/runtime", params={"t": token})
            assert res.status_code == 200
            res = await client.get(f"/apps/{app_slug}/records", headers={"X-Share-Token": token})
            assert res.status_code == 200
            res = await client.get(f"/apps/{app_slug}/records")
            assert res.status_code == 403
            res = await client.get(f"/apps/{app_slug}/form")
            assert res.status_code in (403, 409)  # gated before the form check
            res = await client.post(f"/apps/{app_slug}/form-submit", json={"record": {"name": "x", "mrr": 1}})
            assert res.status_code == 403
            res = await client.post(
                f"/apps/{app_slug}/form-submit", params={"t": token}, json={"record": {"name": "x", "mrr": 1}}
            )
            assert res.status_code == 201

            # disable -> open access again
            res = await client.put(f"/apps/{app_id}/share", json={"enabled": False})
            assert res.status_code == 200 and res.json()["share_token"] is None
            res = await client.get(f"/apps/{app_slug}/runtime")
            assert res.status_code == 200

            # re-enable rotates the token: old links die
            res = await client.put(f"/apps/{app_id}/share", json={"enabled": True})
            new_token = res.json()["share_token"]
            assert new_token != token
            res = await client.get(f"/apps/{app_slug}/runtime", params={"t": token})
            assert res.status_code == 403
            res = await client.get(f"/apps/{app_slug}/runtime", params={"t": new_token})
            assert res.status_code == 200

            # ---- dashboard surface
            res = await client.post("/dashboards", json={
                "name": f"v47-share-dash-{tag}",
                "config": {"components": [
                    {"id": "s1", "type": "stat", "dataset_id": ds_id, "label": "MRR", "agg": "sum", "column": "mrr"},
                ]},
            })
            assert res.status_code == 201, res.text
            dash_id = res.json()["id"]
            dash_slug = res.json()["slug"]
            await client.post(f"/dashboards/{dash_id}/publish")

            res = await client.get(f"/dashboards/{dash_slug}/runtime")
            assert res.status_code == 200  # open until protected
            res = await client.put(f"/dashboards/{dash_id}/share", json={"enabled": True})
            dtok = res.json()["share_token"]
            res = await client.get(f"/dashboards/{dash_slug}/runtime")
            assert res.status_code == 403
            res = await client.get(f"/dashboards/{dash_slug}/runtime", params={"t": dtok})
            assert res.status_code == 200

            await client.delete(f"/apps/{app_id}")
            await client.delete(f"/dashboards/{dash_id}")

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup([], [f"v47-share-{tag}"], [], [], []))


# ---------------------------------------------------------------------------
# 6) Dashboard cross-filtering: ?filter.COL= re-computes every component
# ---------------------------------------------------------------------------
def test_v47_dashboard_cross_filter():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            ds_id = await _mk_dataset(client, f"v47-xfilter-{tag}", [
                {"region": "east", "mrr": 10 + i} if i % 2 else {"region": "west", "mrr": 100 + i}
                for i in range(1, 15)
            ])
            config = {
                "components": [
                    {"id": "c1", "type": "chart", "dataset_id": ds_id, "title": "By region",
                     "chart_type": "donut", "group_by": "region", "agg": "count"},
                    {"id": "k1", "type": "stat", "dataset_id": ds_id, "label": "MRR", "agg": "sum", "column": "mrr"},
                ],
            }
            res = await client.post("/dashboards", json={"name": f"v47-xfilter-{tag}", "config": config})
            assert res.status_code == 201, res.text
            dash_id, dash_slug = res.json()["id"], res.json()["slug"]
            await client.post(f"/dashboards/{dash_id}/publish")

            # unfiltered: both groups present
            res = await client.get(f"/dashboards/{dash_slug}/runtime")
            assert res.status_code == 200
            payload = res.json()
            assert "filters" in payload  # v47: echo key exists even when empty
            donut = next(c for c in payload["components"] if c.get("id") == "c1")
            groups = set(donut["labels"])
            assert groups == {"east", "west"}

            # filtered: every component re-computed over the filtered frame
            res = await client.get(
                f"/dashboards/{dash_slug}/runtime", params={"filter.region": "east"}
            )
            assert res.status_code == 200
            payload = res.json()
            assert payload["filters"] == {"region": ["east"]}
            donut = next(c for c in payload["components"] if c.get("id") == "c1")
            counts = dict(zip(donut["labels"], donut["values"]))
            assert set(counts) == {"east"} and counts["east"] == 7
            kpi = next(c for c in payload["components"] if c.get("id") == "k1")
            assert kpi["value"] == sum(10 + i for i in range(1, 15) if i % 2)

            # comma-multi values work like the apps surface
            res = await client.get(
                f"/dashboards/{dash_slug}/runtime", params={"filter.region": "east,west"}
            )
            donut = next(c for c in res.json()["components"] if c.get("id") == "c1")
            assert len(donut["labels"]) == 2

            await client.delete(f"/dashboards/{dash_id}")

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup([], [f"v47-xfilter-{tag}"], [], [], []))


# ---------------------------------------------------------------------------
# 7) Lineage: engine writes stamp provenance; surface writes stay anonymous
# ---------------------------------------------------------------------------
def test_v47_lineage():
    tag = uuid.uuid4().hex[:8]
    wf_ids, ds_refs = [], []

    async def _go():
        async with _client() as client:
            # workflow-side write: dataset_write creates + replaces the dataset
            rows = [{"id": f"L{i}", "amount": 5.0 * i} for i in range(1, 6)]
            graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("sv", "set_variable", {"assignments": {"items": rows}, "keep_input": False}),
                    _node("dw", "dataset_write", {"dataset": f"v47-lineage-{tag}", "mode": "replace"}),
                ],
                "edges": [_edge("e0", "t", "sv"), _edge("e1", "sv", "dw")],
            }
            wf = await _make_workflow(client, f"v47-lineage-{tag}", graph)
            wf_ids.append(wf)
            exec_row = await _run_and_wait(client, wf)
            assert exec_row["status"] == "success", exec_row.get("error")

            res = await client.get(f"/datasets/{f'v47-lineage-{tag}'}")
            ds_id = res.json()["id"]
            ds_refs.append(f"v47-lineage-{tag}")

            res = await client.get(f"/datasets/{ds_id}/lineage")
            assert res.status_code == 200, res.text
            lineage = res.json()
            assert lineage["name"] == f"v47-lineage-{tag}"
            assert lineage["workflow_versions"] >= 1
            steps = lineage["steps"]
            assert steps, "lineage must have steps"
            wf_steps = [s for s in steps if s["origin"] == "workflow"]
            assert wf_steps, "engine writes must carry provenance"
            for s in wf_steps:
                assert s["workflow_id"] == wf
                assert s["workflow_name"] == f"v47-lineage-{tag}"  # resolved, not a uuid
                assert s["execution_id"] == exec_row["id"]
                assert s["node_name"] == "dw"
            # the version timeline is ascending and matches the write shape
            assert [s["version"] for s in steps] == sorted(s["version"] for s in steps)
            assert steps[-1]["row_count"] == 5

            # surface-side write: API-created dataset has anonymous provenance
            surf_id = await _mk_dataset(client, f"v47-lineage-surf-{tag}", [{"a": 1}, {"a": 2}])
            ds_refs.append(f"v47-lineage-surf-{tag}")
            res = await client.get(f"/datasets/{surf_id}/lineage")
            surf = res.json()
            assert surf["workflow_versions"] == 0
            assert all(s["origin"] == "surface" and s["workflow_id"] is None for s in surf["steps"])

            # 404 on unknown datasets
            res = await client.get("/datasets/ghost/lineage")
            assert res.status_code == 404

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids, ds_refs, [], [], []))
