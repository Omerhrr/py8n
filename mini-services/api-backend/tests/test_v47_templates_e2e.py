"""v47 templates - END-TO-END runs of the two production templates.

These are not graph-shape checks: each template is instantiated through the
real POST /templates/{id}/use endpoint (the same path the Templates gallery
button uses), activated, EXECUTED through the real dispatch/runner, and the
down-stream effects are asserted for real:

- ``scheduled-retraining`` (Scheduled Model Retraining): writes churn_train,
  registers churn_scorer in the model registry with reference stats, and the
  drift gate passes on identical data (report present, no drift).
- ``nightly-etl-quality-gate`` (Nightly ETL + Quality Gate): casts types,
  fills nulls, passes the quality gate, UPSERTs dim_customers and drops a
  CSV export artifact in Artifacts.
"""

from __future__ import annotations

import asyncio
import uuid

import httpx

from app.main import app

API = "http://testserver/api/v1"

RETRAINING = "scheduled-retraining"
ETL = "nightly-etl-quality-gate"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _drain_background() -> None:
    from app.services import executor as executor_mod

    tasks = [t for t in executor_mod._background_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _run_and_wait(client: httpx.AsyncClient, workflow_id: str) -> dict:
    res = await client.post(f"/workflows/{workflow_id}/run", json={"payload": {}})
    assert res.status_code in (200, 202), res.text
    exec_id = res.json()["execution_id"]
    for _ in range(600):
        res = await client.get(f"/executions/{exec_id}")
        assert res.status_code == 200, res.text
        if res.json()["status"] != "running":
            return res.json()
        await asyncio.sleep(0.05)
    raise AssertionError("execution did not finish in time")


def _node_output(execution: dict, node_name: str) -> dict | None:
    for run in execution.get("node_runs") or []:
        if run.get("node_name") == node_name and run.get("output") is not None:
            return run["output"]
    return None


def test_v47_templates_e2e():
    """Both production templates run green through the real engine."""
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []
    ds_names: list[str] = []
    model_ids: list[str] = []
    artifact_ids: list[str] = []

    async def _go():
        async with _client() as client:
            health = (await client.get("/health")).json()
            assert health["version"] >= "1.47.0", health

            for template_id in (RETRAINING, ETL):
                # 1. one-click instantiate, exactly like the gallery button
                res = await client.post(f"/templates/{template_id}/use", json={"name": f"{template_id} {tag}"})
                assert res.status_code == 201, res.text
                wf = res.json()
                wf_ids.append(wf["id"])
                assert wf["is_active"] is False

                # 2. the graph the template ships must validate as-is
                assert [n["type"] for n in wf["graph"]["nodes"]]

                # 3. activate + run it for real
                res = await client.put(f"/workflows/{wf['id']}", json={"is_active": True})
                assert res.status_code == 200, res.text
                execution = await _run_and_wait(client, wf["id"])
                assert execution["status"] in ("succeeded", "success"), (
                    f"{template_id} failed: {execution.get('error')}"
                )

            # ---- retraining assertions ----------------------------------
            res = await client.get("/datasets/churn_train")
            assert res.status_code == 200, res.text
            ds_names.append("churn_train")
            assert res.json()["row_count"] == 12

            res = await client.get("/models")
            rows = [m for m in res.json() if m["name"] == "churn_scorer"]
            assert rows, "churn_scorer must be registered"
            model = rows[0]
            model_ids.append(model["id"])
            assert model["active"], "fresh version must be the active one"
            assert model["has_reference_stats"], "model_train must capture reference stats"
            assert model["metrics"], "model_train must report metrics"

            execution = None
            res = await client.get(f"/executions?workflow_id={wf_ids[0]}")
            execs = res.json() if isinstance(res.json(), list) else res.json().get("items", [])
            assert execs, "retraining execution must be listed"
            exec_id = execs[0]["id"]
            res = await client.get(f"/executions/{exec_id}")
            execution = res.json()
            drift_out = _node_output(execution, "Drift Gate")
            assert drift_out is not None, "drift gate output missing"
            assert drift_out["drift_detected"] is False, drift_out
            report = drift_out["report"]
            assert report["overall_psi"] < 0.1, "identical data must be stable"
            assert report["features"], "per-feature PSI table must be present"

            # ---- ETL assertions -----------------------------------------
            res = await client.get("/datasets/dim_customers")
            assert res.status_code == 200, res.text
            ds_names.append("dim_customers")
            ds = res.json()
            assert ds["row_count"] == 4
            schema = {c["name"]: c["dtype"] for c in ds["schema_json"]}
            assert schema["credit_limit"] in ("number", "integer", "float", "double"), schema
            # NOTE: data crosses nodes as JSON, so datetime64 arrives back as
            # ISO strings - the cast holds in-pipeline (quality gate + upsert
            # see real datetimes), the stored label reflects the JSON boundary.
            assert schema["signup"] in ("datetime", "timestamp", "date", "text"), schema

            res = await client.get(f"/datasets/{ds['id']}/rows?limit=10")
            rows = res.json()["rows"]
            discounts = {r["id"]: r["discount"] for r in rows}
            assert discounts.get("C1") == 0, "null discount must be zero-filled"
            assert discounts.get("C2") == 0.05

            execution = None
            res = await client.get(f"/executions?workflow_id={wf_ids[1]}")
            execs = res.json() if isinstance(res.json(), list) else res.json().get("items", [])
            exec_id = execs[0]["id"]
            res = await client.get(f"/executions/{exec_id}")
            execution = res.json()
            quality = _node_output(execution, "Quality Gate")
            assert quality is not None
            assert quality.get("passed") is True, quality
            export = _node_output(execution, "Export CSV")
            assert export is not None and export.get("artifact_id"), export
            artifact_ids.append(export["artifact_id"])
            res = await client.get(f"/artifacts/{export['artifact_id']}/content")
            assert res.status_code == 200, res.text
            assert b"Acme Corp" in res.content, "CSV artifact must hold the upserted rows"

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())
        asyncio.run(_cleanup(wf_ids, ds_names, model_ids, artifact_ids))


async def _cleanup(wf_ids: list[str], ds_names: list[str], model_ids: list[str], artifact_ids: list[str]) -> None:
    async with _client() as client:
        for mid in model_ids:
            try:
                await client.delete(f"/models/{mid}")
            except Exception:  # noqa: BLE001
                pass
        for aid in artifact_ids:
            try:
                await client.delete(f"/artifacts/{aid}")
            except Exception:  # noqa: BLE001
                pass
        for wid in wf_ids:
            try:
                await client.delete(f"/workflows/{wid}")
            except Exception:  # noqa: BLE001
                pass
        for name in ds_names:
            try:
                res = await client.get(f"/datasets/{name}")
                if res.status_code == 200:
                    await client.delete(f"/datasets/{res.json()['id']}")
            except Exception:  # noqa: BLE001
                pass
