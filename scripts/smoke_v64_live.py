"""V64 live smoke: boot the real server and verify Language Model Systems.

1. Text continued-pretraining: the language-model-system solution installs
   AS A MODEL SYSTEM; its pretrain workflow trains a from-scratch causal
   transformer through the REAL engine (registered as support_lm with
   held-out perplexity), the continued-pretraining workflow adapts it to a
   new corpus (weights + tokenizer carry over, lineage v2), and the
   generation workflow samples text into lm_samples.
2. Model System cockpit: the installed model system reports the language
   paradigm (language_versions, continued_pretrained_versions), text
   modality evidence, and the retraining pipelines.
3. Builder LLM-first + sentiment model system: POST /builder/systems with
   llm_first falls back to the deterministic design with an honest note
   (no bridge in the sandbox); the sentiment model solution installs as a
   model system and trains + serves offline through the same featurizer.

Usage: /home/z/.venv/bin/python scripts/smoke_v64_live.py
"""

from __future__ import annotations

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


def _run_and_wait(c: httpx.Client, wf_id: str) -> dict:
    res = c.post(f"/workflows/{wf_id}/run", json={})
    assert res.status_code in (200, 202), res.text
    ex = res.json()["execution_id"]
    for _ in range(600):
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
    db_path = f"{BACKEND}/data/smoke_v64_{uuid.uuid4().hex[:8]}.sqlite3"
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

            # --- 1) install the language model system + run the LM chain -----
            res = c.post("/solutions/language-model-system/install",
                         json={"as_model_system": True})
            assert res.status_code == 200, res.text
            inst = res.json()
            ms = inst["model_system"]
            assert ms["modalities"] == ["text"]
            wf = {w["name"]: w for w in inst["created_workflows"]}

            run = _run_and_wait(c, wf["Pretrain Language Model"]["id"])
            assert run["status"] == "success", str(run.get("error"))[:400]
            pre = _node_run(run, "Pretrain Language Model")["output"]
            assert pre["mode"] == "from-scratch pretrain"
            assert pre["perplexity"] >= 1.0

            run = _run_and_wait(c, wf["Continue Pretraining"]["id"])
            assert run["status"] == "success", str(run.get("error"))[:400]
            cont = _node_run(run, "Continue Pretraining")["output"]
            assert cont["mode"] == "continued pretrain"
            assert cont["metrics"]["continued_pretrained_from"] == "support_lm v1"

            run = _run_and_wait(c, wf["Generate With Language Model"]["id"])
            assert run["status"] == "success", str(run.get("error"))[:400]
            gen = _node_run(run, "Generate Text")["output"]
            assert isinstance(gen["text"], str) and len(gen["text"]) > 0

            res = c.get("/models")
            versions = [m for m in res.json() if m["name"] == "support_lm"]
            assert len(versions) == 2
            checks += 1
            print(f"[1] LM chain offline: pretrain ppl={pre['perplexity']} -> "
                  f"continued ppl={cont['perplexity']} (lineage {cont['metrics']['continued_pretrained_from']}) -> "
                  f"generated {gen['tokens_generated']} tokens: {gen['text'][:48]!r}")

            # --- 2) the model system cockpit reports the language paradigm ---
            # models are created by TRAINING (after install) - attach them like
            # any curated member, then the derived sections pick them up
            res = c.get("/models")
            for m in [m for m in res.json() if m["name"] == "support_lm"]:
                r = c.post(f"/model-systems/{ms['id']}/components",
                           json={"kind": "model", "ref_id": m["id"]})
                assert r.status_code == 201, r.text
            detail = c.get(f"/model-systems/{ms['id']}").json()
            assert detail["training"]["language_versions"] == 2
            assert detail["training"]["continued_pretrained_versions"] == 1
            assert "text" in detail["modalities"]["evidence"]
            assert len(detail["retraining"]) == 2  # the two lm_train pipelines
            assert detail["evaluation"] and "perplexity" in detail["evaluation"][0]["metrics"]
            checks += 1
            print(f"[2] model system cockpit: language={detail['training']['language_versions']} "
                  f"continued={detail['training']['continued_pretrained_versions']} "
                  f"retraining={len(detail['retraining'])} pipelines, "
                  f"evidence={detail['modalities']['evidence']}")

            # --- 3) builder LLM-first fallback + sentiment model system ------
            res = c.post("/builder/systems", json={
                "description": "every hour pull orders from postgres, dedupe them "
                               "and alert me if quality drops",
                "llm_first": True})
            assert res.status_code == 201, res.text
            spec = res.json()["spec"]
            assert spec["mode"] == "llm_first_fallback"
            assert any("fell back" in n for n in spec["notes"])
            selected = {cmp["id"] for cmp in spec["components"] if cmp["selected"]}
            assert {"target_dataset", "pipeline_workflow"} <= selected

            res = c.post("/solutions/sentiment-model-system/install",
                         json={"as_model_system": True})
            assert res.status_code == 200, res.text
            inst_s = res.json()
            wf_s = {w["name"]: w for w in inst_s["created_workflows"]}
            run = _run_and_wait(c, wf_s["Train Sentiment Model"]["id"])
            assert run["status"] == "success", str(run.get("error"))[:400]
            run = _run_and_wait(c, wf_s["Serve Sentiment Scorer"]["id"])
            assert run["status"] == "success", str(run.get("error"))[:400]
            scored = _node_run(run, "Score Sentiment")["output"]["items"]
            assert all(s["prediction"] in ("positive", "negative") for s in scored)
            checks += 1
            print(f"[3] llm_first fallback + sentiment model system: "
                  f"spec mode={spec['mode']}, predictions={[s['prediction'] for s in scored]}")

            print(f"SMOKE v64 GREEN - {checks}/3 checks passed")
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
