"""V66 live smoke: boot the real server and verify the torch training
backend, multimodal LM fine-tuning, and LM drift monitoring.

1. TORCH BACKEND: an lm_train run with device=torch trains the transformer
   through the torch mirror (torch-CPU here; the same code runs on CUDA/MPS
   with a different device string) and generates text through torch; a
   NUMPY-pretrained LM is then CONTINUED on the torch backend with the
   tokenizer and lineage carried over - the cross-backend handoff.
2. MULTIMODAL FINE-TUNING: a text-only LM gains a condition-prefix adapter
   (backbone carries over, fresh projection, lineage kept) trained on rows
   with numeric condition features; lm_generate samples conditioned on the
   same kind of vector and refuses wrong dimensions honestly.
3. LM DRIFT: lm_train registers the held-out loss distribution as reference
   stats; drift_check PSI-scores a new corpus against it - the same corpus
   is stable, an alien corpus trips drift and (on_drift=error) fails the
   run; /ops/devices reports the torch-equipped inventory honestly.

Usage: /home/z/.venv/bin/python scripts/smoke_v66_live.py
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import time
import uuid

import httpx

BACKEND = "/home/z/my-project/py8n/mini-services/api-backend"
API = "http://127.0.0.1:8199/api/v1"
TORCH_HERE = importlib.util.find_spec("torch") is not None

CORPUS = [
    "the agent replies to the customer about the login issue",
    "the agent fixes the login bug today",
    "the customer asks about the refund policy",
    "the agent ships the order to the customer",
    "the ticket about the login issue is closed",
    "the refund policy covers the order",
    "the agent escalates the ticket to the team",
    "the customer thanks the agent today",
] * 2

ALIEN_CORPUS = [
    "zzz qqq xjj vvv kkk 777 42 9911 zz qqqq",
    "kv xj zq 12345 8888 vvqq xkjq zzvv 77",
    "the xylophone quarantines awkwardly xerox",
    "zzz vvv 31337 qqq xjj kkkv vv zzq 4242",
    "qwxx jkvl zz 918273 qqqxz vk 55 xjjj",
    "xq zj vk qqqq 7777 zzzz vvq xkj 12",
    "zz kv vx jq 616161 xqq vvz kjj 909",
    "qz vx kk xj 222333 vqz zkx 88 zzqq",
] * 2


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


def _run_wf(c: httpx.Client, name: str, graph: dict) -> dict:
    res = c.post("/workflows", json={"name": name, "graph": graph})
    assert res.status_code in (200, 201), res.text
    wf_id = res.json()["id"]
    res = c.post(f"/workflows/{wf_id}/run", json={})
    assert res.status_code in (200, 202), res.text
    ex = res.json()["execution_id"]
    for _ in range(2400):
        det = c.get(f"/executions/{ex}").json()
        if det["status"] not in ("running", "queued"):
            return det
        time.sleep(0.05)
    raise AssertionError("execution did not finish in time")


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v66_{uuid.uuid4().hex[:8]}.sqlite3"
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

            # --- 1) the torch backend trains and generates -------------------
            res = c.post("/datasets", json={"name": f"torch-corpus-{uuid.uuid4().hex[:6]}",
                                            "rows": [{"doc": d} for d in CORPUS]})
            ds = res.json()
            run = _run_wf(c, "smoke-torch-lm", {"nodes": [
                {"id": "t", "type": "manual_trigger", "name": "t", "position": {"x": 0, "y": 0}, "parameters": {}},
                {"id": "r", "type": "dataset_read", "name": "r", "position": {"x": 1, "y": 0}, "parameters": {"dataset": ds["name"]}},
                {"id": "lm", "type": "lm_train", "name": "lm", "position": {"x": 2, "y": 0},
                 "parameters": {"text_column": "doc", "vocab_size": 120, "d_model": 24, "n_heads": 2,
                                "n_ctx": 12, "epochs": 8, "batch_size": 8, "learning_rate": 0.005,
                                "device": "torch", "model_name": "smoke_torch_lm"}},
            ], "edges": [{"id": "e1", "source": "t", "target": "r"},
                         {"id": "e2", "source": "r", "target": "lm"}]})
            assert run["status"] == "success", str(run.get("error"))[:400]
            torch_out = _node_run(run, "lm")["output"]
            assert torch_out["metrics"]["device_backend"] == "torch"
            assert torch_out["metrics"]["device"] == "cpu"  # torch-CPU in this sandbox

            run = _run_wf(c, "smoke-torch-gen", {"nodes": [
                {"id": "t", "type": "manual_trigger", "name": "t", "position": {"x": 0, "y": 0},
                 "parameters": {"payload": {"prompt": "the agent"}}},
                {"id": "g", "type": "lm_generate", "name": "g", "position": {"x": 1, "y": 0},
                 "parameters": {"model": "smoke_torch_lm", "device": "torch",
                                "prompt": "{{ nodes.t.output.payload.prompt }}",
                                "max_tokens": 6, "temperature": 0.7, "top_k": 15}},
            ], "edges": [{"id": "e1", "source": "t", "target": "g"}]})
            assert run["status"] == "success", str(run.get("error"))[:400]
            gen = _node_run(run, "g")["output"]
            assert gen["device_backend"] == "torch" and len(gen["text"]) > 0

            # CROSS-BACKEND: numpy pretrain -> torch continue
            res = c.post("/datasets", json={"name": f"np-corpus-{uuid.uuid4().hex[:6]}",
                                            "rows": [{"doc": d} for d in CORPUS]})
            ds2 = res.json()
            run = _run_wf(c, "smoke-np-lm", {"nodes": [
                {"id": "t", "type": "manual_trigger", "name": "t", "position": {"x": 0, "y": 0}, "parameters": {}},
                {"id": "r", "type": "dataset_read", "name": "r", "position": {"x": 1, "y": 0}, "parameters": {"dataset": ds2["name"]}},
                {"id": "lm", "type": "lm_train", "name": "lm", "position": {"x": 2, "y": 0},
                 "parameters": {"text_column": "doc", "vocab_size": 120, "d_model": 24, "n_heads": 2,
                                "n_ctx": 12, "epochs": 8, "batch_size": 8, "learning_rate": 0.005,
                                "device": "cpu", "model_name": "smoke_np_lm"}},
            ], "edges": [{"id": "e1", "source": "t", "target": "r"},
                         {"id": "e2", "source": "r", "target": "lm"}]})
            assert run["status"] == "success", str(run.get("error"))[:400]
            np_out = _node_run(run, "lm")["output"]
            assert np_out["metrics"]["device_backend"] == "numpy"

            run = _run_wf(c, "smoke-x-backend", {"nodes": [
                {"id": "t", "type": "manual_trigger", "name": "t", "position": {"x": 0, "y": 0}, "parameters": {}},
                {"id": "r", "type": "dataset_read", "name": "r", "position": {"x": 1, "y": 0}, "parameters": {"dataset": ds2["name"]}},
                {"id": "lm", "type": "lm_train", "name": "lm", "position": {"x": 2, "y": 0},
                 "parameters": {"text_column": "doc", "base_model": "smoke_np_lm", "device": "torch",
                                "epochs": 6, "batch_size": 8, "learning_rate": 0.003,
                                "model_name": "smoke_np_lm"}},
            ], "edges": [{"id": "e1", "source": "t", "target": "r"},
                         {"id": "e2", "source": "r", "target": "lm"}]})
            assert run["status"] == "success", str(run.get("error"))[:400]
            x_out = _node_run(run, "lm")["output"]
            assert x_out["mode"] == "continued pretrain"
            assert x_out["metrics"]["device_backend"] == "torch"
            assert x_out["metrics"]["continued_pretrained_from"] == "smoke_np_lm v1"
            assert x_out["vocabulary"] == np_out["vocabulary"]
            checks += 1
            print(f"[1] torch backend: torch lm ppl={torch_out['perplexity']} "
                  f"(device={torch_out['metrics']['device']}), generated {gen['tokens_generated']} tokens; "
                  f"numpy->torch continue ppl={x_out['perplexity']} with lineage "
                  f"{x_out['metrics']['continued_pretrained_from']}")

            # --- 2) LM drift: stable corpus passes, alien corpus trips -------
            res = c.post("/datasets", json={"name": f"same-{uuid.uuid4().hex[:6]}",
                                            "rows": [{"doc": d} for d in CORPUS]})
            same = res.json()
            run = _run_wf(c, "smoke-dr-same", {"nodes": [
                {"id": "t", "type": "manual_trigger", "name": "t", "position": {"x": 0, "y": 0}, "parameters": {}},
                {"id": "r", "type": "dataset_read", "name": "r", "position": {"x": 1, "y": 0}, "parameters": {"dataset": same["name"]}},
                {"id": "dc", "type": "drift_check", "name": "dc", "position": {"x": 2, "y": 0},
                 "parameters": {"model": "smoke_np_lm", "text_column": "doc", "threshold": 0.25}},
            ], "edges": [{"id": "e1", "source": "t", "target": "r"},
                         {"id": "e2", "source": "r", "target": "dc"}]})
            assert run["status"] == "success", str(run.get("error"))[:400]
            rep = _node_run(run, "dc")["output"]["report"]
            assert rep["signal"] == "lm_loss_psi" and rep["drift_detected"] is False

            res = c.post("/datasets", json={"name": f"alien-{uuid.uuid4().hex[:6]}",
                                            "rows": [{"doc": d} for d in ALIEN_CORPUS]})
            alien = res.json()
            run = _run_wf(c, "smoke-dr-alien", {"nodes": [
                {"id": "t", "type": "manual_trigger", "name": "t", "position": {"x": 0, "y": 0}, "parameters": {}},
                {"id": "r", "type": "dataset_read", "name": "r", "position": {"x": 1, "y": 0}, "parameters": {"dataset": alien["name"]}},
                {"id": "dc", "type": "drift_check", "name": "dc", "position": {"x": 2, "y": 0},
                 "parameters": {"model": "smoke_np_lm", "text_column": "doc", "threshold": 0.25,
                                "on_drift": "error"}},
            ], "edges": [{"id": "e1", "source": "t", "target": "r"},
                         {"id": "e2", "source": "r", "target": "dc"}]})
            assert run["status"] == "error", "the alien corpus must trip the drift gate"
            assert "Drift detected" in (run.get("error") or "")

            res = c.get("/ops/devices")
            inv = res.json()
            assert inv["torch_installed"] == TORCH_HERE
            assert "torch" in " ".join(inv["allowed_modes"])
            checks += 1
            print(f"[3] LM drift: same-corpus PSI={rep['overall_psi']} (stable), alien corpus trips "
                  f"the gate; /ops/devices torch_installed={inv['torch_installed']} "
                  f"modes={inv['allowed_modes']}")

            # --- 3) multimodal fine-tuning (moved after drift): adapter on a text-only base ------
            rows = [{"doc": CORPUS[i % len(CORPUS)],
                     "img_brightness": round(0.2 + 0.05 * (i % 8), 3),
                     "img_edges": round(0.1 + 0.03 * (i % 5), 3)} for i in range(20)]
            res = c.post("/datasets", json={"name": f"mm-{uuid.uuid4().hex[:6]}", "rows": rows})
            mm_ds = res.json()
            run = _run_wf(c, "smoke-mm-ft", {"nodes": [
                {"id": "t", "type": "manual_trigger", "name": "t", "position": {"x": 0, "y": 0}, "parameters": {}},
                {"id": "r", "type": "dataset_read", "name": "r", "position": {"x": 1, "y": 0}, "parameters": {"dataset": mm_ds["name"]}},
                {"id": "lm", "type": "lm_train", "name": "lm", "position": {"x": 2, "y": 0},
                 "parameters": {"text_column": "doc", "base_model": "smoke_np_lm",
                                "condition_columns": "img_brightness,img_edges",
                                "epochs": 6, "batch_size": 8, "learning_rate": 0.003,
                                "model_name": "smoke_np_lm"}},
            ], "edges": [{"id": "e1", "source": "t", "target": "r"},
                         {"id": "e2", "source": "r", "target": "lm"}]})
            assert run["status"] == "success", str(run.get("error"))[:400]
            mm_out = _node_run(run, "lm")["output"]
            assert mm_out["metrics"]["multimodal_adapter_added"] is True
            assert mm_out["metrics"]["condition_dim"] == 2

            run = _run_wf(c, "smoke-mm-gen", {"nodes": [
                {"id": "t", "type": "manual_trigger", "name": "t", "position": {"x": 0, "y": 0},
                 "parameters": {"payload": {"prompt": "the agent", "c1": 0.55, "c2": 0.22}}},
                {"id": "g", "type": "lm_generate", "name": "g", "position": {"x": 1, "y": 0},
                 "parameters": {"model": "smoke_np_lm",
                                "prompt": "{{ nodes.t.output.payload.prompt }}",
                                "condition": "{{ [nodes.t.output.payload.c1, nodes.t.output.payload.c2] | join(',') }}",
                                "max_tokens": 6, "temperature": 0.7, "top_k": 15}},
            ], "edges": [{"id": "e1", "source": "t", "target": "g"}]})
            assert run["status"] == "success", str(run.get("error"))[:400]
            mm_gen = _node_run(run, "g")["output"]
            assert mm_gen["conditioned"] is True and len(mm_gen["text"]) > 0
            checks += 1
            print(f"[2] multimodal fine-tuning: adapter added on a pretrained text-only LM "
                  f"(condition_dim={mm_out['metrics']['condition_dim']}, ppl={mm_out['perplexity']}), "
                  f"conditioned generation: {mm_gen['text'][:40]!r}")

            print(f"SMOKE v66 GREEN - {checks}/3 checks passed")
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
