"""V46 feature tests: deep data estate II - data science + apps.

Data science: model registry (versioned, activatable, owner-scoped) + the
new model_train v2 (9 algorithms, impute/scale/one-hot preprocessing pipeline
pickled WITH the model, cross-validation, stratified splits, confusion matrix,
rmse) + model_predict (batch scoring by name → active version, prediction +
probability columns). Apps: kpi/markdown/filter components, scatter/line/area/
donut charts, server-side compute (preview endpoint + runtime components with
filter params), records search/sort, form → workflow actions. Dashboards:
area/donut chart types + configurable refresh_seconds.

Runs the FastAPI app in-process via httpx ASGITransport (same harness as v4-v45).
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


async def _cleanup(client_unused: None, workflow_ids: list[str], dataset_refs: list[str], app_refs: list[str], dashboard_ids: list[str], model_ids: list[str]) -> None:
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


def _clf_rows(n: int = 80, seed: int = 7) -> list[dict]:
    import random

    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        group = rng.choice(["A", "B"])
        rows.append({
            "x1": round(rng.gauss(2 if group == "A" else -2, 1.0), 3),
            "x2": round(rng.gauss(0, 1.0), 3),
            "city": rng.choice(["lagos", "cairo", "nairobi"]),
            "tier": group,
        })
    return rows


def _reg_rows(n: int = 80, seed: int = 3) -> list[dict]:
    import random

    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        x1 = rng.uniform(0, 10)
        x2 = rng.uniform(0, 5)
        rows.append({"x1": round(x1, 3), "x2": round(x2, 3), "y": round(2 * x1 + 0.5 * x2 + rng.gauss(0, 0.3), 3)})
    return rows


# ---------------------------------------------------------------------------
# 1) Definitions: 46 types, model_predict exposed, version pinned
# ---------------------------------------------------------------------------
def test_v46_definitions():
    async def _go():
        async with _client() as client:
            res = await client.get("/health")
            assert res.json()["version"] == "1.46.0"
            res = await client.get("/node-definitions")
            defs = res.json()["definitions"]
            types = [d["type"] for d in defs]
            assert len(types) == 46, f"expected 46 visible types at v46, got {len(types)}"
            by = {d["type"]: d for d in defs}
            assert "model_predict" in types
            props = set(by["model_predict"]["parameters_schema"]["properties"].keys())
            assert props == {"model", "probability_column"}
            # model_train grew the v46 params
            mt_props = set(by["model_train"]["parameters_schema"]["properties"].keys())
            assert {"task", "scale", "cross_validation", "hyperparams", "model_name", "random_state"} <= mt_props
            assert len(by["model_train"]["parameters_schema"]["properties"]["model"]["options"]) == 9

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(None, [], [], [], [], []))


# ---------------------------------------------------------------------------
# 2) model_train v2: registry registration, confusion matrix, CV, rmse
# ---------------------------------------------------------------------------
def test_v46_model_train_v2():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            model_ids: list[str] = []
            # --- classification: registry + confusion matrix + proba-shaped metrics
            graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("m", "model_train", {
                        "model": "random_forest_classifier",
                        "target": "tier",
                        "model_name": f"churn-{tag}",
                        "cross_validation": 3,
                    }),
                ],
                "edges": [_edge("e1", "t", "m")],
            }
            wf = await _make_workflow(client, f"v46-train-clf-{tag}", graph)
            exec_row = await _run_and_wait(client, wf, {"items": _clf_rows()})
            assert exec_row["status"] == "success", exec_row.get("error")
            out = _find_node_run(exec_row, "m")["output"]
            assert out["task"] == "classification"
            metrics = out["metrics"]
            assert "accuracy" in metrics and "precision_weighted" in metrics and "recall_weighted" in metrics
            assert set(metrics["confusion_matrix"]["labels"]) == {"A", "B"}
            assert metrics.get("cv_mean") is not None and metrics["cv_folds"] == 3
            reg = out["registry"]
            assert reg["name"] == f"churn-{tag}" and reg["version"] == 1 and reg["active"] is True
            model_ids.append(reg["id"])

            # registry API: list + get
            res = await client.get("/models")
            rows = [r for r in res.json() if r["name"] == f"churn-{tag}"]
            assert len(rows) == 1 and rows[0]["active"] is True and rows[0]["task"] == "classification"

            # --- second version: same name → version 2 becomes active
            graph2 = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("m", "model_train", {
                        "model": "logistic_regression",
                        "target": "tier",
                        "model_name": f"churn-{tag}",
                    }),
                ],
                "edges": [_edge("e1", "t", "m")],
            }
            wf2 = await _make_workflow(client, f"v46-train-clf2-{tag}", graph2)
            exec_row2 = await _run_and_wait(client, wf2, {"items": _clf_rows(seed=11)})
            out2 = _find_node_run(exec_row2, "m")["output"]
            reg2 = out2["registry"]
            assert reg2["version"] == 2 and reg2["active"] is True
            model_ids.append(reg2["id"])
            res = await client.get(f"/models/{model_ids[0]}")
            assert res.json()["active"] is False  # v1 was deactivated

            # --- regression: rmse + coefficients-style attribution
            graph3 = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("m", "model_train", {"model": "linear_regression", "target": "y", "model_name": f"price-{tag}"}),
                ],
                "edges": [_edge("e1", "t", "m")],
            }
            wf3 = await _make_workflow(client, f"v46-train-reg-{tag}", graph3)
            exec_row3 = await _run_and_wait(client, wf3, {"items": _reg_rows()})
            out3 = _find_node_run(exec_row3, "m")["output"]
            assert out3["task"] == "regression"
            assert out3["metrics"]["r2"] > 0.9
            assert "rmse" in out3["metrics"]
            model_ids.append(out3["registry"]["id"])

            # task/algorithm mismatch is a clean node error
            graph4 = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("m", "model_train", {"model": "linear_regression", "target": "tier"}),
                ],
                "edges": [_edge("e1", "t", "m")],
            }
            wf4 = await _make_workflow(client, f"v46-train-bad-{tag}", graph4)
            exec_row4 = await _run_and_wait(client, wf4, {"items": _clf_rows()})
            assert exec_row4["status"] == "error"
            assert "is a regressor but the task is classification" in (exec_row4.get("error") or "")

            # unsupported hyperparameter is rejected
            graph5 = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("m", "model_train", {"model": "random_forest_classifier", "target": "tier", "hyperparams": {"evil_kwarg": 1}}),
                ],
                "edges": [_edge("e1", "t", "m")],
            }
            wf5 = await _make_workflow(client, f"v46-train-badhp-{tag}", graph5)
            exec_row5 = await _run_and_wait(client, wf5, {"items": _clf_rows()})
            assert exec_row5["status"] == "error"
            assert "not supported" in (exec_row5.get("error") or "")

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(None, [], [], [], [], []))


# ---------------------------------------------------------------------------
# 3) model registry API: activate / delete
# ---------------------------------------------------------------------------
def test_v46_model_registry_api():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            # two versions via two trainings
            ids: list[str] = []
            for i, algo in enumerate(("random_forest_classifier", "decision_tree_classifier")):
                graph = {
                    "nodes": [
                        _node("t", "manual_trigger"),
                        _node("m", "model_train", {"model": algo, "target": "tier", "model_name": f"api-{tag}"}),
                    ],
                    "edges": [_edge("e1", "t", "m")],
                }
                wf = await _make_workflow(client, f"v46-api-train{i}-{tag}", graph)
                exec_row = await _run_and_wait(client, wf, {"items": _clf_rows(seed=5 + i)})
                ids.append(_find_node_run(exec_row, "m")["output"]["registry"]["id"])

            # activate the older version
            res = await client.post(f"/models/{ids[0]}/activate")
            assert res.status_code == 200 and res.json()["active"] is True
            res = await client.get(f"/models/{ids[1]}")
            assert res.json()["active"] is False

            # delete the active one → the other can be re-activated
            res = await client.delete(f"/models/{ids[0]}")
            assert res.status_code == 204
            res = await client.get(f"/models/{ids[0]}")
            assert res.status_code == 404
            res = await client.post(f"/models/{ids[1]}/activate")
            assert res.json()["active"] is True

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(None, [], [], [], [], []))


# ---------------------------------------------------------------------------
# 4) model_predict: batch scoring by name → active version
# ---------------------------------------------------------------------------
def test_v46_model_predict_node():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            ds_id = await _mk_dataset(client, f"v46-score-{tag}", _clf_rows())
            train_graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("d", "dataset_read", {"dataset": f"v46-score-{tag}", "limit": 0}),
                    _node("m", "model_train", {"model": "random_forest_classifier", "target": "tier", "model_name": f"scorer-{tag}"}),
                ],
                "edges": [_edge("e1", "t", "d"), _edge("e2", "d", "m")],
            }
            wf = await _make_workflow(client, f"v46-predict-train-{tag}", train_graph)
            exec_row = await _run_and_wait(client, wf)
            assert exec_row["status"] == "success", exec_row.get("error")
            model_info = _find_node_run(exec_row, "m")["output"]["registry"]

            # predict: dataset_read → model_predict (by NAME → active version)
            predict_graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("d", "dataset_read", {"dataset": f"v46-score-{tag}", "limit": 40}),
                    _node("p", "model_predict", {"model": f"scorer-{tag}"}),
                ],
                "edges": [_edge("e0", "t", "d"), _edge("e1", "d", "p")],
            }
            wf2 = await _make_workflow(client, f"v46-predict-run-{tag}", predict_graph)
            exec_row2 = await _run_and_wait(client, wf2)
            assert exec_row2["status"] == "success", exec_row2.get("error")
            out = _find_node_run(exec_row2, "p")["output"]
            assert out["predicted"] == 40 and out["rows_in"] == 40
            assert out["model"]["id"] == model_info["id"] and out["model"]["version"] == model_info["version"]
            first = out["items"][0]
            assert first["prediction"] in ("A", "B")
            assert 0.0 <= first["prediction_proba"] <= 1.0

            # missing feature column → clean error
            predict_graph2 = {
                "nodes": [
                    _node("d", "dataset_read", {"dataset": f"v46-score-{tag}", "limit": 5}),
                    _node("p", "model_predict", {"model": f"scorer-{tag}"}),
                    # score rows LACK x1: strip via summarize passthrough? simpler: second dataset
                ],
                "edges": [_edge("e1", "d", "p")],
            }
            # rows without the feature columns
            bad = await _mk_dataset(client, f"v46-score-bad-{tag}", [{"city": "lagos", "tier": "A"}] * 20)
            predict_graph3 = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("d", "dataset_read", {"dataset": f"v46-score-bad-{tag}", "limit": 10}),
                    _node("p", "model_predict", {"model": f"scorer-{tag}"}),
                ],
                "edges": [_edge("e0", "t", "d"), _edge("e1", "d", "p")],
            }
            wf3 = await _make_workflow(client, f"v46-predict-bad-{tag}", predict_graph3)
            exec_row3 = await _run_and_wait(client, wf3)
            assert exec_row3["status"] == "error"
            assert "needs feature column" in (exec_row3.get("error") or "")

            # unknown model → clean error
            predict_graph4 = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("d", "dataset_read", {"dataset": f"v46-score-{tag}", "limit": 5}),
                    _node("p", "model_predict", {"model": f"nope-{tag}"}),
                ],
                "edges": [_edge("e0", "t", "d"), _edge("e1", "d", "p")],
            }
            wf4 = await _make_workflow(client, f"v46-predict-unk-{tag}", predict_graph4)
            exec_row4 = await _run_and_wait(client, wf4)
            assert exec_row4["status"] == "error"
            assert "not found in the registry" in (exec_row4.get("error") or "")

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(None, [], [], [], [], []))


# ---------------------------------------------------------------------------
# 5) apps: new components, server-side runtime + filters + preview
# ---------------------------------------------------------------------------
def test_v46_apps_deep_components():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            ds_id = await _mk_dataset(client, f"v46-app-{tag}", [
                {"name": f"c{i}", "plan": ["free", "pro"][i % 2], "mrr": 10 + i, "days": i}
                for i in range(1, 21)
            ])
            config = {
                "workflow_id": None,
                "components": [
                    {"id": "k1", "type": "kpi", "label": "MRR", "agg": "sum", "column": "mrr"},
                    {"id": "k2", "type": "stat", "label": "People", "agg": "count_distinct", "column": "name"},
                    {"id": "c1", "type": "chart", "title": "By plan", "chart_type": "donut", "group_by": "plan", "agg": "count"},
                    {"id": "c2", "type": "chart", "title": "MRR vs days", "chart_type": "scatter", "x": "days", "y": "mrr"},
                    {"id": "md1", "type": "markdown", "body": "## Hello\n**bold** and `code` and <script>alert(1)</script>"},
                    {"id": "f1", "type": "filter", "column": "plan", "label": "Plan"},
                    {"id": "t1", "type": "table", "title": "Records", "columns": ["name", "plan", "mrr"], "page_size": 5},
                ],
            }
            res = await client.post("/apps", json={"name": f"v46-app-{tag}", "dataset_id": ds_id, "config": config})
            assert res.status_code == 201, res.text
            app_row = res.json()
            res = await client.post(f"/apps/{app_row['id']}/publish")
            assert res.status_code == 200, res.text
            slug = app_row["slug"]

            # runtime: every component rendered server-side
            res = await client.get(f"/apps/{slug}/runtime")
            body = res.json()
            comps = {c["id"]: c for c in body["components"]}
            assert comps["k1"]["value"] == sum(range(11, 31))  # mrr sum
            assert comps["k2"]["value"] == 20  # count_distinct
            assert comps["c1"]["chart_type"] == "donut" and set(comps["c1"]["labels"]) == {"free", "pro"}
            assert len(comps["c2"]["points"]) == 20
            assert "<strong>bold</strong>" in comps["md1"]["html"]
            assert "<script>" not in comps["md1"]["html"]  # XSS-safe markdown
            assert set(comps["f1"]["options"]) == {"free", "pro"}
            assert comps["t1"]["total"] == 20 and len(comps["t1"]["rows"]) == 5

            # filter param actually filters (i odd → pro: mrr 11,13,…,29 = 200)
            res = await client.get(f"/apps/{slug}/runtime?filter.plan=pro")
            comps2 = {c["id"]: c for c in res.json()["components"]}
            assert comps2["k1"]["value"] == 200
            assert comps2["t1"]["total"] == 10

            # builder preview endpoint mirrors the runtime (i even → free: 12,14,…,30 = 210)
            res = await client.post(f"/apps/{app_row['id']}/preview", json={"components": config["components"], "filters": {"plan": ["free"]}})
            assert res.status_code == 200
            comps3 = {c["id"]: c for c in res.json()["components"]}
            assert comps3["k1"]["value"] == 210

            # bad component → clean validation error
            res = await client.post(f"/apps/{app_row['id']}/preview", json={"components": [{"id": "x", "type": "chart", "chart_type": "hologram", "group_by": "plan"}]})
            assert res.status_code == 400

            # records: server-side search + sort
            res = await client.get(f"/apps/{slug}/records?q=pro&sort_by=mrr&sort_dir=desc")
            rows = res.json()["rows"]
            assert rows and all(r["plan"] == "pro" for r in rows)
            mrrs = [r["mrr"] for r in rows]
            assert mrrs == sorted(mrrs, reverse=True)

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(None, [], [], [], [], []))


# ---------------------------------------------------------------------------
# 6) apps: form → workflow action
# ---------------------------------------------------------------------------
def test_v46_form_workflow_action():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            ds_id = await _mk_dataset(client, f"v46-form-{tag}", [
                {"name": "seed", "note": "x"},
            ])
            # workflow with a webhook-less manual trigger that just records input
            wf_graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("s", "set_variable", {"assignments": {"got": "{{ input | tojson }}"}, "keep_input": False}),
                ],
                "edges": [_edge("e1", "t", "s")],
            }
            wf_id = await _make_workflow(client, f"v46-form-wf-{tag}", wf_graph)

            config = {
                "workflow_id": wf_id,
                "components": [
                    {"id": "form1", "type": "form", "title": "Add", "fields": ["name", "note"], "submit_label": "Go"},
                ],
            }
            res = await client.post("/apps", json={"name": f"v46-form-app-{tag}", "dataset_id": ds_id, "config": config})
            assert res.status_code == 201, res.text
            slug = res.json()["slug"]
            res = await client.post(f"/apps/{slug}/publish")
            assert res.status_code == 200

            # standalone form submit fires the bound workflow
            res = await client.post(f"/apps/{slug}/form-submit", json={"record": {"name": "alice", "note": "hi"}})
            assert res.status_code == 201, res.text
            body = res.json()
            assert body["workflow_triggered"] is True

            # the triggered execution exists with the app_form trigger
            execs = await client.get("/executions?limit=50")
            rows = execs.json() if isinstance(execs.json(), list) else execs.json().get("items", [])
            triggered = [r for r in rows if r.get("trigger_type") == "app_form"]
            assert triggered, rows[:3]
            assert triggered[0]["workflow_id"] == wf_id

            # nonexistent workflow_id is rejected at config time
            res = await client.post("/apps", json={"name": f"v46-form-bad-{tag}", "dataset_id": ds_id, "config": {"workflow_id": "nope", "components": []}})
            assert res.status_code == 404

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(None, [], [], [], [], []))


# ---------------------------------------------------------------------------
# 7) dashboards: area/donut charts + refresh_seconds
# ---------------------------------------------------------------------------
def test_v46_dashboards_upgrades():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            ds_id = await _mk_dataset(client, f"v46-dash-{tag}", [
                {"region": "east" if i % 2 else "west", "mrr": 10 + i} for i in range(1, 15)
            ])
            config = {
                "refresh_seconds": 120,
                "components": [
                    {"id": "c1", "type": "chart", "dataset_id": ds_id, "title": "By region", "chart_type": "donut", "group_by": "region", "agg": "count"},
                    {"id": "c2", "type": "chart", "dataset_id": ds_id, "title": "MRR", "chart_type": "area", "group_by": "region", "agg": "sum", "column": "mrr"},
                ],
            }
            res = await client.post("/dashboards", json={"name": f"v46-dash-{tag}", "config": config})
            assert res.status_code == 201, res.text
            dash_id = res.json()["id"]

            # bad refresh rejected
            res = await client.post("/dashboards", json={"name": f"v46-dash-bad-{tag}", "config": {**config, "refresh_seconds": 3}})
            assert res.status_code == 400
            res = await client.post("/dashboards", json={"name": f"v46-dash-bad2-{tag}", "config": {**config, "chart_type": "hologram", "components": [{"id": "x", "type": "chart", "dataset_id": ds_id, "chart_type": "hologram", "group_by": "region", "agg": "count"}]}})
            assert res.status_code == 400

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(None, [], [], [], [], []))
