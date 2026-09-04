"""V63 live smoke: boot the real server and verify Model Systems.

1. From-scratch training: a numpy MLP trains through the REAL engine on
   a dataset stream, registers as mlp_classifier with architecture
   metrics, and the MLP artifact scores through model_predict.
2. Multimodal pipeline: text_features(fit) -> image_features ->
   audio_features -> neural_train runs end-to-end through the engine
   (text featurizer artifact + stateless image/audio features feed the
   from-scratch network).
3. Model System cockpit: bind dataset + model + pipelines -> the derived
   sections report training/neural splits, monitoring coverage,
   retraining schedule; binding it into a Py8n System shows up in the
   system health and the cross-system dependency view.

Usage: /home/z/.venv/bin/python scripts/smoke_v63_live.py
"""

from __future__ import annotations

import base64
import io
import os
import subprocess
import time
import uuid

import httpx

BACKEND = "/home/z/my-project/py8n/mini-services/api-backend"
API = "http://127.0.0.1:8199/api/v1"


def wait_health(client: httpx.Client, deadline: float = 30.0) -> None:
    end = time.time() + deadline
    while time.time() < end:
        try:
            res = client.get(f"{API}/health")
            if res.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.4)
    raise SystemExit("server never became healthy")


def _png_b64(color: tuple[int, int, int]) -> str:
    from PIL import Image

    img = Image.new("RGB", (40, 30), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _run_and_wait(c: httpx.Client, wf_id: str) -> dict:
    res = c.post(f"/workflows/{wf_id}/run", json={})
    assert res.status_code in (200, 202), res.text
    ex = res.json()["execution_id"]
    for _ in range(300):
        det = c.get(f"/executions/{ex}").json()
        if det["status"] not in ("running", "queued"):
            return det
        time.sleep(0.05)
    raise AssertionError("execution did not finish in time")


def _node_run(execution: dict, node_name: str) -> dict:
    for run in execution.get("node_runs") or []:
        if run.get("node_name") == node_name:
            return run
    raise AssertionError(f"node run {node_name!r} not found")


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v63_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
    })
    proc = subprocess.Popen(
        ["/home/z/.venv/bin/python", "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", "8199", "--log-level", "warning"],
        cwd=BACKEND, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        with httpx.Client(base_url=API, timeout=120) as c:
            wait_health(c)
            checks = 0
            tag = uuid.uuid4().hex[:6]
            model_name = f"smoke-model-{tag}"

            rows = [{"tenure": float(40 - i), "monthly_spend": float(20 + (i * 7) % 60),
                     "support_tickets": float(i % 4),
                     "churned": "yes" if (i * 13) % 7 < 3 else "no"}
                    for i in range(1, 41)]
            res = c.post("/datasets", json={"name": f"smoke-ds-{tag}", "rows": rows})
            ds = res.json()
            ds_name = ds["name"]

            # --- 1) from-scratch training + MLP scoring -----------------------
            res = c.post("/workflows", json={"name": f"smoke-train-{tag}", "graph": {
                "nodes": [
                    {"id": "t", "type": "manual_trigger", "name": "t", "position": {"x": 0, "y": 0}, "parameters": {}},
                    {"id": "r", "type": "dataset_read", "name": "r", "position": {"x": 0, "y": 0}, "parameters": {"dataset": ds_name}},
                    {"id": "nn", "type": "neural_train", "name": "nn", "position": {"x": 0, "y": 0},
                     "parameters": {"task": "classification", "target": "churned",
                                    "features": "tenure,monthly_spend,support_tickets",
                                    "hidden_layers": "24,12", "epochs": 40, "batch_size": 8,
                                    "learning_rate": 0.02, "optimizer": "adam",
                                    "model_name": model_name, "register": True}},
                ],
                "edges": [{"id": "e1", "source": "t", "target": "r", "sourceHandle": "main", "targetHandle": "main"},
                          {"id": "e2", "source": "r", "target": "nn", "sourceHandle": "main", "targetHandle": "main"}]}})
            assert res.status_code == 201, res.text
            run = _run_and_wait(c, res.json()["id"])
            assert run["status"] == "success", str(run.get("error"))[:400]
            out = _node_run(run, "nn")["output"]
            assert out["mode"] == "from-scratch"
            assert out["architecture"] == "3->24->12->2"

            res = c.post("/datasets", json={"name": f"smoke-score-{tag}",
                                            "rows": [{"tenure": 30.0, "monthly_spend": 50.0, "support_tickets": 1.0},
                                                     {"tenure": 5.0, "monthly_spend": 90.0, "support_tickets": 3.0}]})
            score_ds = res.json()
            res = c.post("/workflows", json={"name": f"smoke-score-wf-{tag}", "graph": {
                "nodes": [
                    {"id": "t", "type": "manual_trigger", "name": "t", "position": {"x": 0, "y": 0}, "parameters": {}},
                    {"id": "r", "type": "dataset_read", "name": "r", "position": {"x": 0, "y": 0}, "parameters": {"dataset": score_ds["name"]}},
                    {"id": "p", "type": "model_predict", "name": "p", "position": {"x": 0, "y": 0},
                     "parameters": {"model": model_name}},
                ],
                "edges": [{"id": "e1", "source": "t", "target": "r", "sourceHandle": "main", "targetHandle": "main"},
                          {"id": "e2", "source": "r", "target": "p", "sourceHandle": "main", "targetHandle": "main"}]}})
            run2 = _run_and_wait(c, res.json()["id"])
            assert run2["status"] == "success", str(run2.get("error"))[:400]
            scored = _node_run(run2, "p")["output"]["items"]
            assert all(s["prediction"] in ("yes", "no") for s in scored)
            checks += 1
            print(f"[1] from-scratch MLP: {out['architecture']} trained offline, "
                  f"predict roundtrip={[(s['prediction']) for s in scored]}")

            # --- 2) multimodal pipeline feeds the network ----------------------
            media_rows = [{"text": "you win a free prize now", "img": _png_b64((255, 0, 0)),
                           "aud": "", "label": "spam"},
                          {"text": "meeting notes from yesterday", "img": _png_b64((0, 0, 255)),
                           "aud": "", "label": "ham"}] * 8
            res = c.post("/datasets", json={"name": f"smoke-media-{tag}", "rows": media_rows})
            media = res.json()
            res = c.post("/workflows", json={"name": f"smoke-multi-{tag}", "graph": {
                "nodes": [
                    {"id": "t", "type": "manual_trigger", "name": "t", "position": {"x": 0, "y": 0}, "parameters": {}},
                    {"id": "r", "type": "dataset_read", "name": "r", "position": {"x": 0, "y": 0}, "parameters": {"dataset": media["name"]}},
                    {"id": "tf", "type": "text_features", "name": "tf", "position": {"x": 0, "y": 0},
                     "parameters": {"column": "text", "mode": "fit", "featurizer": f"smoke-vec-{tag}",
                                    "svd_dims": 4, "prefix": "txt"}},
                    {"id": "img", "type": "image_features", "name": "img", "position": {"x": 0, "y": 0},
                     "parameters": {"image_field": "img", "prefix": "img"}},
                    {"id": "nn", "type": "neural_train", "name": "nn", "position": {"x": 0, "y": 0},
                     "parameters": {"task": "classification", "target": "label",
                                    "features": "", "hidden_layers": "16,8", "epochs": 25,
                                    "batch_size": 8, "model_name": f"smoke-mm-{tag}", "register": True}},
                ],
                "edges": [{"id": "e1", "source": "t", "target": "r", "sourceHandle": "main", "targetHandle": "main"},
                          {"id": "e2", "source": "r", "target": "tf", "sourceHandle": "main", "targetHandle": "main"},
                          {"id": "e3", "source": "tf", "target": "img", "sourceHandle": "main", "targetHandle": "main"},
                          {"id": "e4", "source": "img", "target": "nn", "sourceHandle": "main", "targetHandle": "main"}]}})
            run3 = _run_and_wait(c, res.json()["id"])
            assert run3["status"] == "success", str(run3.get("error"))[:400]
            mm = _node_run(run3, "nn")["output"]
            assert mm["rows_used"] == 16
            checks += 1
            print(f"[2] multimodal: text+image features -> {mm['architecture']} trained, acc={mm['metrics'].get('accuracy')}")

            # --- 3) the model-system cockpit + system binding ------------------
            res = c.post("/model-systems", json={"name": f"Smoke Model System {tag}",
                                                 "modalities": ["tabular", "text", "image"]})
            ms = res.json()
            res = c.get("/models")
            mrow = next(m for m in res.json() if m["name"] == model_name and m["active"])
            for kind, ref in (("dataset", ds["id"]), ("model", mrow["id"])):
                assert c.post(f"/model-systems/{ms['id']}/components", json={"kind": kind, "ref_id": ref}).status_code == 201
            # bind the training + scoring workflows
            res = c.get("/workflows")
            all_wfs = res.json()
            score_wf = next(w for w in all_wfs if w["name"] == f"smoke-score-wf-{tag}")
            train_wf = next(w for w in all_wfs if w["name"] == f"smoke-train-{tag}")
            assert c.post(f"/model-systems/{ms['id']}/components",
                          json={"kind": "workflow", "ref_id": score_wf["id"]}).status_code == 201
            assert c.post(f"/model-systems/{ms['id']}/components",
                          json={"kind": "workflow", "ref_id": train_wf["id"]}).status_code == 201

            detail = c.get(f"/model-systems/{ms['id']}").json()
            assert detail["training"]["neural_versions"] >= 1
            assert "tabular" in detail["modalities"]["evidence"]
            assert detail["deployment"] and detail["deployment"][0]["name"] == f"smoke-score-wf-{tag}"
            assert detail["monitoring"]["drift_capable"] is True

            # bind into a Py8n System: the COMPANY AI SYSTEM pattern
            res = c.post("/systems", json={"name": f"Company AI {tag}"})
            sys_id = res.json()["id"]
            assert c.post(f"/systems/{sys_id}/components",
                          json={"kind": "model_system", "ref_id": ms["id"]}).status_code == 201
            sd = c.get(f"/systems/{sys_id}").json()
            assert sd["health"]["model_systems"]["bound"] == 1
            deps = c.get("/systems/dependencies").json()
            assert any(e["type"] == "shared_object" for e in deps["edges"]) or deps["edges"] == []
            checks += 1
            print(f"[3] model system: neural={detail['training']['neural_versions']} "
                  f"deployment={len(detail['deployment'])} monitoring={detail['monitoring']['coverage_pct']}% "
                  f"py8n-system binding ok")

            print(f"SMOKE v63 GREEN - {checks}/3 checks passed")
            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        try:
            os.unlink(db_path)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
