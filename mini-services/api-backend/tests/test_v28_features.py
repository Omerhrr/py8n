"""V28 feature tests: Data Science Workbench - python_transform, chart, model_train.

* python_transform: real pandas/numpy over the input items as `df`; whitelisted
  imports (pandas/numpy/sklearn + stdlib math/statistics/datetime/random/...);
  stdout captured as `logs`; executor timeout.
* chart: matplotlib (Agg) PNG saved as an ARTIFACT - GET /artifacts/{id}/content
  serves the bytes the executions drawer renders inline.
* model_train: curated sklearn models - regression/classification metrics,
  prediction sample, pickled model artifact; honest guards (min rows, target
  column, numeric features).

Runs the FastAPI app in-process via httpx ASGITransport (same harness as v4-v27).
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


async def _cleanup(workflow_ids: list[str], dataset_refs: list[str], artifact_ids: list[str]) -> None:
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
        for aid in artifact_ids:
            try:
                await client.delete(f"/artifacts/{aid}")
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
    for _ in range(150):
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


def _ml_rows(n: int = 60, seed: int = 7) -> list[dict]:
    import random

    rnd = random.Random(seed)
    rows = []
    for _ in range(n):
        x1 = rnd.uniform(0, 10)
        x2 = rnd.uniform(0, 5)
        y = 2 * x1 + 0.5 * x2 + rnd.gauss(0, 0.25)
        rows.append({"x1": round(x1, 3), "x2": round(x2, 3), "y": round(y, 3)})
    return rows


def _clf_rows(n: int = 60, seed: int = 3) -> list[dict]:
    import random

    rnd = random.Random(seed)
    rows = []
    for i in range(n):
        if i % 2 == 0:
            a, b, label = rnd.gauss(2.0, 0.4), rnd.gauss(1.0, 0.4), "A"
        else:
            a, b, label = rnd.gauss(6.0, 0.4), rnd.gauss(5.0, 0.4), "B"
        rows.append({"a": round(a, 3), "b": round(b, 3), "label": label})
    return rows


# ---------------------------------------------------------------------------
# 1) Definitions: the three DS nodes exposed
# ---------------------------------------------------------------------------
def test_v28_definitions():
    async def _go():
        async with _client() as client:
            res = await client.get("/node-definitions")
            assert res.status_code == 200
            defs = res.json()["definitions"]
            types = [d["type"] for d in defs]
            assert len(types) == 37, f"expected 37 visible types, got {len(types)}"
            by = {d["type"]: d for d in defs}
            for t in ("python_transform", "chart", "model_train"):
                assert t in types, t
            props = {t: set(by[t]["parameters_schema"]["properties"].keys()) for t in ("python_transform", "chart", "model_train")}
            assert props["python_transform"] == {"code", "timeout_seconds"}
            assert props["chart"] == {"chart_type", "x", "y", "title", "color"}
            assert props["model_train"] == {"model", "target", "features", "test_size"}
            assert by["chart"]["parameters_schema"]["properties"]["chart_type"]["options"] == ["bar", "line", "scatter", "hist", "pie"]

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup([], [], []))


# ---------------------------------------------------------------------------
# 2) python_transform: pandas ops, logs, import whitelist, error surfacing
# ---------------------------------------------------------------------------
def test_v28_python_transform_node():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []

    async def _go():
        async with _client() as client:
            rows = [
                {"city": "Lagos", "ltv": 1240.5}, {"city": "Berlin", "ltv": 9800.0},
                {"city": "Seoul", "ltv": 0.0}, {"city": "Paris", "ltv": 1985.0},
            ]
            graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("py", "python_transform", {
                        "code": (
                            "df['big'] = df.ltv > 100\n"
                            "result = df[df.big][['city', 'ltv']]\n"
                            "print('kept', len(result), 'of', len(df))\n"
                        )
                    }, "Clean"),
                    _node("s", "set_variable", {"keep_input": False, "assignments": {
                        "n": "{{ input.rows_out }}", "first": "{{ input.items[0].city }}",
                    }}, "Out"),
                ],
                "edges": [_edge("e1", "t", "py"), _edge("e2", "py", "s")],
            }
            wid = await _make_workflow(client, f"v28 py {tag}", graph)
            wf_ids.append(wid)
            run = await _run_and_wait(client, wid, payload={"items": rows})
            assert run["status"] == "success", run.get("error")

            py = _find_node_run(run, "Clean")
            assert py["status"] == "success"
            assert [it["city"] for it in py["output"]["items"]] == ["Lagos", "Berlin", "Paris"]
            assert py["output"]["rows_in"] == 4 and py["output"]["rows_out"] == 3
            assert "kept 3 of 4" in py["output"]["logs"]
            assert _find_node_run(run, "Out")["output"] == {"n": 3, "first": "Lagos"}

            # numpy + sklearn import path
            graph2 = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("py", "python_transform", {
                        "code": (
                            "from sklearn.preprocessing import StandardScaler\n"
                            "import numpy as np\n"
                            "z = StandardScaler().fit_transform(df[['ltv']])\n"
                            "result = df.assign(zscore=np.round(z[:, 0], 3))\n"
                        )
                    }, "Scale"),
                ],
                "edges": [_edge("e1", "t", "py")],
            }
            wid2 = await _make_workflow(client, f"v28 py sklearn {tag}", graph2)
            wf_ids.append(wid2)
            run2 = await _run_and_wait(client, wid2, payload={"items": rows})
            assert run2["status"] == "success", run2.get("error")
            items = _find_node_run(run2, "Scale")["output"]["items"]
            assert abs(sum(it["zscore"] for it in items)) < 0.01  # standardized

            # guards: blocked import / bad code
            for i, code in enumerate((
                "import os\nresult = df",
                "result = df.missing_method()",
            )):
                g = {"nodes": [_node("t", "manual_trigger"), _node("py", "python_transform", {"code": code})],
                     "edges": [_edge("e1", "t", "py")]}
                w = await _make_workflow(client, f"v28 py bad{i} {tag}", g)
                wf_ids.append(w)
                r = await _run_and_wait(client, w, payload={"items": rows})
                assert r["status"] == "error", r.get("error")
                err = r.get("error") or ""
                if i == 0:
                    assert "not allowed" in err and "os" in err
                else:
                    assert "AttributeError" in err

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids, [], []))


# ---------------------------------------------------------------------------
# 3) chart: bar artifact served as PNG; pie/hist variants; guards
# ---------------------------------------------------------------------------
def test_v28_chart_node():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []
    artifact_ids: list[str] = []

    async def _go():
        async with _client() as client:
            rows = [
                {"city": "Lagos", "ltv": 1240.5}, {"city": "Berlin", "ltv": 9800.0},
                {"city": "Seoul", "ltv": 120.0}, {"city": "Paris", "ltv": 1985.0},
            ]
            graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("c", "chart", {"chart_type": "bar", "x": "city", "y": "ltv", "title": f"LTV {tag}"}, "Bar"),
                ],
                "edges": [_edge("e1", "t", "c")],
            }
            wid = await _make_workflow(client, f"v28 chart {tag}", graph)
            wf_ids.append(wid)
            run = await _run_and_wait(client, wid, payload={"items": rows})
            assert run["status"] == "success", run.get("error")

            out = _find_node_run(run, "Bar")["output"]
            assert out["chart_type"] == "bar" and out["points"] == 4
            assert out["artifact_id"] in out["artifact_url"]
            artifact_ids.append(out["artifact_id"])

            # the artifact round-trips as a real PNG
            res = await client.get(f"/artifacts/{out['artifact_id']}/content")
            assert res.status_code == 200
            assert res.headers["content-type"].startswith("image/png")
            assert res.content[:4] == b"\x89PNG" and len(res.content) > 5000

            meta = (await client.get(f"/artifacts/{out['artifact_id']}")).json()
            assert meta["kind"] == "chart" and meta["meta"]["title"] == f"LTV {tag}"

            # hist + line with two series
            g2 = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("h", "chart", {"chart_type": "hist", "y": "ltv", "title": "dist"}, "Hist"),
                    _node("l", "chart", {"chart_type": "line", "x": "city", "y": "ltv,v", "title": "multi"}, "Line"),
                ],
                "edges": [_edge("e1", "t", "h"), _edge("e2", "t", "l")],
            }
            # v needs to exist for the line chart
            rows2 = [dict(r, v=r["ltv"] / 2) for r in rows]
            w2 = await _make_workflow(client, f"v28 chart multi {tag}", g2)
            wf_ids.append(w2)
            r2 = await _run_and_wait(client, w2, payload={"items": rows2})
            assert r2["status"] == "success", r2.get("error")
            for name in ("Hist", "Line"):
                o = _find_node_run(r2, name)["output"]
                artifact_ids.append(o["artifact_id"])

            # guards: no y / unknown column / pie with two y / empty input
            g3 = {"nodes": [_node("t", "manual_trigger"), _node("c", "chart", {"y": "nope"})],
                  "edges": [_edge("e1", "t", "c")]}
            w3 = await _make_workflow(client, f"v28 chart bad {tag}", g3)
            wf_ids.append(w3)
            r3 = await _run_and_wait(client, w3, payload={"items": rows})
            assert r3["status"] == "error" and "not found" in (r3.get("error") or "")

            g4 = {"nodes": [_node("t", "manual_trigger"), _node("c", "chart", {"chart_type": "pie", "x": "city", "y": "ltv,v"})],
                  "edges": [_edge("e1", "t", "c")]}
            w4 = await _make_workflow(client, f"v28 chart pie2 {tag}", g4)
            wf_ids.append(w4)
            r4 = await _run_and_wait(client, w4, payload={"items": rows2})
            assert r4["status"] == "error" and "exactly one" in (r4.get("error") or "")

            r5 = await _run_and_wait(client, w3, payload={"items": []})
            assert r5["status"] == "error" and "needs input items" in (r5.get("error") or "")

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids, [], artifact_ids))


# ---------------------------------------------------------------------------
# 4) model_train: regression r2 + classifier accuracy + guards
# ---------------------------------------------------------------------------
def test_v28_model_train_node():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []
    artifact_ids: list[str] = []

    async def _go():
        async with _client() as client:
            rows = _ml_rows(60)
            graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("m", "model_train", {"model": "linear_regression", "target": "y", "features": "x1,x2"}, "Fit"),
                    _node("s", "set_variable", {"keep_input": False, "assignments": {
                        "r2": "{{ input.metrics.r2 }}", "mid": "{{ input.model_id }}",
                    }}, "Out"),
                ],
                "edges": [_edge("e1", "t", "m"), _edge("e2", "m", "s")],
            }
            wid = await _make_workflow(client, f"v28 fit {tag}", graph)
            wf_ids.append(wid)
            run = await _run_and_wait(client, wid, payload={"items": rows})
            assert run["status"] == "success", run.get("error")

            out = _find_node_run(run, "Fit")["output"]
            assert out["metrics"]["r2"] > 0.9, out["metrics"]
            assert abs(out["metrics"]["coefficients"]["x1"] - 2.0) < 0.2
            assert len(out["items"]) <= 20 and "actual" in out["items"][0]
            assert out["rows_used"] == 60 and out["features"] == ["x1", "x2"]
            artifact_ids.append(out["model_id"])
            assert _find_node_run(run, "Out")["output"]["mid"] == out["model_id"]

            # model pickle round-trips
            res = await client.get(f"/artifacts/{out['model_id']}/content")
            assert res.status_code == 200 and res.headers["content-type"].startswith("application/octet-stream")
            import pickle

            model = pickle.loads(res.content)
            assert hasattr(model, "predict")

            # classifier with text labels
            g2 = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("m", "model_train", {"model": "random_forest_classifier", "target": "label"}, "Clf"),
                ],
                "edges": [_edge("e1", "t", "m")],
            }
            w2 = await _make_workflow(client, f"v28 clf {tag}", g2)
            wf_ids.append(w2)
            run2 = await _run_and_wait(client, w2, payload={"items": _clf_rows(60)})
            assert run2["status"] == "success", run2.get("error")
            out2 = _find_node_run(run2, "Clf")["output"]
            assert out2["metrics"]["accuracy"] >= 0.9, out2["metrics"]
            assert "feature_importances" in out2["metrics"]
            assert out2["items"][0]["actual"] in ("A", "B")
            artifact_ids.append(out2["model_id"])

            # guards: too few rows / unknown target / no numeric features
            for i, (params, data) in enumerate((
                ({"model": "linear_regression", "target": "y"}, rows[:5]),
                ({"model": "linear_regression", "target": "zzz"}, rows[:20]),
                ({"model": "linear_regression", "target": "city"}, [{"city": "Lagos"}, {"city": "Berlin"}] * 10),
            )):
                g = {"nodes": [_node("t", "manual_trigger"), _node("m", "model_train", params)],
                     "edges": [_edge("e1", "t", "m")]}
                w = await _make_workflow(client, f"v28 fit bad{i} {tag}", g)
                wf_ids.append(w)
                r = await _run_and_wait(client, w, payload={"items": data})
                assert r["status"] == "error", r.get("error")
                err = r.get("error") or ""
                assert ("at least 10" in err) or ("not found" in err) or ("numeric feature" in err)

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids, [], artifact_ids))


# ---------------------------------------------------------------------------
# 5) artifacts API: list + filter + delete (content 404 after)
# ---------------------------------------------------------------------------
def test_v28_artifacts_api():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []
    artifact_ids: list[str] = []

    async def _go():
        async with _client() as client:
            g = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("c", "chart", {"chart_type": "bar", "x": "city", "y": "ltv", "title": f"api {tag}"}, "Bar"),
                ],
                "edges": [_edge("e1", "t", "c")],
            }
            wid = await _make_workflow(client, f"v28 art {tag}", g)
            wf_ids.append(wid)
            run = await _run_and_wait(client, wid, payload={
                "items": [{"city": "Lagos", "ltv": 10}, {"city": "Berlin", "ltv": 20}],
            })
            out = _find_node_run(run, "Bar")["output"]
            artifact_ids.append(out["artifact_id"])

            res = await client.get("/artifacts", params={"kind": "chart"})
            assert res.status_code == 200
            listed = {a["id"] for a in res.json()}
            assert out["artifact_id"] in listed
            assert all(a["kind"] == "chart" for a in res.json())
            assert res.json()[0]["url"].endswith("/content")

            res = await client.delete(f"/artifacts/{out['artifact_id']}")
            assert res.status_code == 204
            assert (await client.get(f"/artifacts/{out['artifact_id']}")).status_code == 404
            assert (await client.get(f"/artifacts/{out['artifact_id']}/content")).status_code == 404
            artifact_ids.remove(out["artifact_id"])

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids, [], artifact_ids))


# ---------------------------------------------------------------------------
# 6) FULL DS PIPELINE: dataset -> transform -> chart -> model (one workflow)
# ---------------------------------------------------------------------------
def test_v28_end_to_end_pipeline():
    tag = uuid.uuid4().hex[:8]
    ds_name = f"v28 pipeline {tag}"
    wf_ids: list[str] = []
    dataset_refs: list[str] = []
    artifact_ids: list[str] = []

    async def _go():
        async with _client() as client:
            res = await client.post("/datasets", json={"name": ds_name, "rows": _ml_rows(60)})
            assert res.status_code == 201, res.text
            dataset_refs.append(res.json()["id"])

            graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("r", "dataset_read", {"dataset": ds_name, "limit": 0}, "Load"),
                    _node("py", "python_transform", {"code": "result = df[df.x1 > 1.0]"}, "Clean"),
                    _node("c", "chart", {"chart_type": "scatter", "x": "x1", "y": "y", "title": "y vs x1"}, "Scatter"),
                    _node("m", "model_train", {"model": "linear_regression", "target": "y", "features": "x1,x2", "test_size": 0.25}, "Fit"),
                ],
                "edges": [
                    _edge("e1", "t", "r"), _edge("e2", "r", "py"),
                    _edge("e3", "py", "c"), _edge("e4", "c", "m"),
                ],
            }
            wid = await _make_workflow(client, f"v28 pipeline {tag}", graph)
            wf_ids.append(wid)
            run = await _run_and_wait(client, wid)
            assert run["status"] == "success", run.get("error")

            loads = _find_node_run(run, "Load")["output"]
            assert loads["row_count"] == 60 and loads["returned"] == 60

            clean = _find_node_run(run, "Clean")["output"]
            assert clean["rows_in"] == 60
            assert 50 <= clean["rows_out"] < 60  # filter drops the x1 < 1.0 tail

            scatter = _find_node_run(run, "Scatter")["output"]
            assert scatter["chart_type"] == "scatter" and scatter["points"] == clean["rows_out"]
            artifact_ids.append(scatter["artifact_id"])

            fit = _find_node_run(run, "Fit")["output"]
            assert fit["metrics"]["r2"] > 0.9, fit["metrics"]
            assert fit["rows_used"] == clean["rows_out"]
            artifact_ids.append(fit["model_id"])
            print("pipeline r2:", fit["metrics"]["r2"])

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids, dataset_refs, artifact_ids))
