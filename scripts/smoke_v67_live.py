"""V67 live smoke: boot the real server and verify the three platform fronts.

1. LARGER-CONTEXT TORCH: lm_train with device=torch trains at n_ctx=128
   (torch-CPU here; the same code runs on CUDA/MPS) and lm_generate samples
   32 tokens with the sliding-window metadata; the numpy backend honestly
   refuses TRAINING beyond 64 while still SERVING the big model.
2. ARCHITECTURE LAYERS: the builder synthesizes a staging + dead-letter +
   schema-contract pipeline from one sentence; the run lands raw rows in
   {name} staging, quarantines the dtype violation into {name} dead letter
   stamped with _dl_reasons/_dl_at, and the curated dataset gets only the
   clean rows; the system detail derives the medallion layer map.
3. DEPLOY + PLATFORM: the torch-trained LM becomes a LIVE deployment whose
   generated webhook endpoint answers a POST with generated text (served
   through the numpy core - state parity across backends); GET /platform
   then reads all five verbs (compose/build/train/deploy/operate) as
   active and reports the platform READY.

Usage: /home/z/.venv/bin/python scripts/smoke_v67_live.py
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid

import httpx

BACKEND = "/home/z/my-project/py8n/mini-services/api-backend"
API = "http://127.0.0.1:8199/api/v1"
TORCH_HERE = True  # the venv has torch; if absent, check 1's torch leg is skipped

CORPUS = [
    "the support agent resolved the ticket about the login issue",
    "the agent fixed the login bug and the customer left a review",
    "the customer asked about the refund policy for the order",
    "the agent shipped the order and closed the ticket today",
    "the ticket about the refund was escalated to the team",
    "the customer thanked the agent for the quick fix",
    "the login issue returned and the agent reprovisioned access",
    "the order arrived late so the agent applied a refund",
] * 6  # ~48 docs so a 128-token context sees real repetition


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


def _node_run(execution: dict, node_name: str) -> dict:
    for run in execution.get("node_runs") or []:
        if run.get("node_name") == node_name:
            return run
    raise AssertionError(f"node run {node_name!r} not found")


def _run_wf(c: httpx.Client, name: str, graph: dict, payload: dict | None = None) -> dict:
    res = c.post("/workflows", json={"name": name, "graph": graph})
    assert res.status_code in (200, 201), res.text
    wf_id = res.json()["id"]
    res = c.post(f"/workflows/{wf_id}/run", json={"payload": payload or {}})
    assert res.status_code in (200, 202), res.text
    ex = res.json()["execution_id"]
    for _ in range(4000):
        det = c.get(f"/executions/{ex}").json()
        if det["status"] not in ("running", "queued"):
            return det
        time.sleep(0.05)
    raise AssertionError("execution did not finish in time")


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v67_{uuid.uuid4().hex[:8]}.sqlite3"
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
        with httpx.Client(base_url=API, timeout=600) as c:
            wait_health(c)
            checks = 0

            # --- 1) larger-context torch training + sliding generation ------
            corpus = [{"doc": d} for d in CORPUS]
            ds_name = f"smoke-corpus-{uuid.uuid4().hex[:6]}"
            res = c.post("/datasets", json={"name": ds_name, "rows": corpus})
            assert res.status_code == 201, res.text

            run = _run_wf(c, "smoke-ctx", {"nodes": [
                {"id": "t", "type": "manual_trigger", "name": "t", "position": {"x": 0, "y": 0}, "parameters": {}},
                {"id": "r", "type": "dataset_read", "name": "r", "position": {"x": 1, "y": 0}, "parameters": {"dataset": ds_name}},
                {"id": "lm", "type": "lm_train", "name": "lm", "position": {"x": 2, "y": 0},
                 "parameters": {"text_column": "doc", "d_model": 24, "n_heads": 2,
                                "n_ctx": 128, "epochs": 4, "batch_size": 8, "learning_rate": 0.005,
                                "device": "torch", "model_name": "smoke_ctx_lm"}},
            ], "edges": [{"id": "e1", "source": "t", "target": "r"},
                         {"id": "e2", "source": "r", "target": "lm"}]})
            assert run["status"] == "success", str(run.get("error"))[:400]
            out = _node_run(run, "lm")["output"]
            assert out["metrics"]["context_length"] == 128, out["metrics"]
            assert out["metrics"]["device_backend"] == "torch"
            print(f"[1] torch ctx-128 train ok: ppl={out['perplexity']} "
                  f"({out['metrics']['params_count']} params, {out['metrics']['train_seconds']}s)")

            run = _run_wf(c, "smoke-gen", {"nodes": [
                {"id": "t", "type": "manual_trigger", "name": "t", "position": {"x": 0, "y": 0}, "parameters": {}},
                {"id": "g", "type": "lm_generate", "name": "g", "position": {"x": 1, "y": 0},
                 "parameters": {"model": "smoke_ctx_lm", "prompt": "the agent",
                                "max_tokens": 32, "device": "cpu"}},
            ], "edges": [{"id": "e1", "source": "t", "target": "g"}]})
            assert run["status"] == "success", str(run.get("error"))[:400]
            gen = _node_run(run, "g")["output"]
            assert gen["tokens_generated"] == 32 and gen["context_window"] == 128
            print(f"[1] generation ok: {gen['tokens_generated']} tokens at ctx {gen['context_window']} "
                  f"(numpy serving a torch artifact): {gen['text'][:60]!r}")

            # honest numpy refusal: TRAINING at ctx 128 on the numpy core
            run = _run_wf(c, "smoke-refused", {"nodes": [
                {"id": "t", "type": "manual_trigger", "name": "t", "position": {"x": 0, "y": 0}, "parameters": {}},
                {"id": "lm2", "type": "lm_train", "name": "lm2", "position": {"x": 1, "y": 0},
                 "parameters": {"text_column": "doc", "n_ctx": 128, "device": "cpu", "epochs": 1}},
            ], "edges": [{"id": "e1", "source": "t", "target": "lm2"}]},
                payload={"items": corpus})
            assert run["status"] == "error" and "torch backend" in (run.get("error") or "")
            print("[1] numpy ctx-128 training refused loudly (honest device policy)")
            checks += 1

            # --- 2) builder architecture layers: staging + dead letter ------
            res = c.post("/builder/systems", json={
                "description": "Land raw invoice rows into a staging area with a schema contract, "
                               "quarantine bad rows in a dead letter queue, keep the clean rows in "
                               "the curated table."})
            assert res.status_code == 201, res.text
            draft = res.json()
            picked = {comp["id"] for comp in draft["spec"]["components"] if comp["selected"]}
            assert {"staging_layer", "dead_letter_queue", "schema_contract"} <= picked, picked
            res = c.post(f"/builder/systems/{draft['id']}/answers",
                         json={"answers": {"fields": "amount:number,product:text"}})
            assert res.status_code == 200, res.text
            res = c.post(f"/builder/systems/{draft['id']}/components",
                         json={"component_id": "schedule", "selected": False})
            assert res.status_code == 200, res.text
            res = c.post(f"/builder/systems/{draft['id']}/build", json={"as_system": True})
            assert res.status_code == 200, res.text
            built = res.json()["built"]
            assert built["on_violation"] == "dead_letter"
            assert set(built["layers"]) == {"staging", "curated", "dead_letter"}

            res = c.put(f"/workflows/{built['workflow_id']}", json={"is_active": True})
            assert res.status_code in (200, 204), res.text
            rows = [{"amount": 12, "product": "widget"},
                    {"amount": 5, "product": "gadget"},
                    {"amount": "abc", "product": "broken"}]
            # run the BUILT pipeline through the real engine
            res = c.post(f"/workflows/{built['workflow_id']}/run", json={"payload": {"items": rows}})
            assert res.status_code == 200, res.text
            ex = res.json()["execution_id"]
            for _ in range(4000):
                det = c.get(f"/executions/{ex}").json()
                if det["status"] not in ("running", "queued"):
                    break
                time.sleep(0.05)
            assert det["status"] == "success", str(det.get("error"))[:400]

            res = c.get(f"/datasets/{built['staging_dataset_id']}/rows").json()
            assert len(res["rows"]) == 3, "staging must hold ALL raw rows"
            res = c.get(f"/datasets/{built['dataset_id']}/rows").json()
            assert sorted(r["product"] for r in res["rows"]) == ["gadget", "widget"], res["rows"]
            res = c.get(f"/datasets/{built['dead_letter_dataset_id']}/rows").json()
            assert len(res["rows"]) == 1 and res["rows"][0]["product"] == "broken"
            assert "amount:dtype" in res["rows"][0]["_dl_reasons"]
            assert res["rows"][0]["_dl_at"]
            dl_row = res["rows"][0]

            res = c.get(f"/systems/{built['system_id']}").json()
            layers = {l["layer"] for l in res["architecture"]["layers"]}
            assert layers == {"staging", "curated", "dead_letter"}, layers
            print("[2] medallion run ok: staging=3 curated=2 dead_letter=1 "
                  f"(_dl_reasons={dl_row['_dl_reasons']})")
            checks += 1

            # --- 3) deploy the LM + the platform reads READY ------------------
            res = c.post("/deployments", json={
                "name": "smoke ctx endpoint", "model": "smoke_ctx_lm", "environment": "prod",
                "max_tokens": 24})
            assert res.status_code == 201, res.text
            dep = res.json()
            assert dep["status"] == "live" and dep["serving_mode"] == "generate"

            res = c.post(f"/webhooks/{dep['workflow']['id']}", json={"prompt": "the customer and the agent"})
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["status"] == "success"
            last = body["last_output"]
            assert last["tokens_generated"] >= 1 and last["text"].strip()
            res = c.get(f"/deployments/{dep['id']}")
            assert res.json()["stats"]["runs_7d"] >= 1
            print(f"[3] deployment serving ok: {last['tokens_generated']} tokens over HTTP "
                  f"({dep['model']['name']} v{dep['model']['version']}): {last['text'][:50]!r}")

            res = c.get("/platform")
            assert res.status_code == 200, res.text
            platform = res.json()
            verdicts = platform["verdicts"]
            assert all(verdicts.values()), verdicts
            assert platform["ready"] is True
            assert platform["deploying"]["serving_invocations_7d"] >= 1
            print(f"[3] platform ready: compose={platform['composing']['systems']} system(s) "
                  f"build={platform['building']['built']} train={platform['training']['registry_versions']} "
                  f"deploy={platform['deploying']['live']} live endpoint(s) "
                  f"operate={platform['operating']['executions_7d']} runs/7d")
            checks += 1

            print(f"\nSMOKE GREEN: {checks}/3 checks passed (larger-context torch, "
                  "staging/dead-letter layers, deployment + platform ready)")
            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(db_path + suffix)
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
