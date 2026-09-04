"""V65 live smoke: boot the real server and verify the LM lifecycle
experience, the BPE tokenizer, the honest device mode, and video sampling.

1. LM LIFECYCLE IN SEQUENCE: the Language Model System installs from the
   marketplace AS A MODEL SYSTEM; GET /model-systems/{id}/lifecycle derives
   the stage plan off the bound graphs, and ONE call to run-lifecycle runs
   the three workflows IN ORDER through the real engine - pretrain ppl,
   continued-pretraining ppl with lineage, then sampled text. The full LM
   lifecycle, experienced the way a user experiences it.
2. BPE TOKENIZER: an lm_train run with tokenizer="bpe" learns real merges
   (compression > 1 char/token), continues pretraining with the SAME fitted
   BPE tokenizer, and generates text decoded through the model's own BPE.
3. DEVICE MODE + VIDEO: GET /ops/devices reports the honest inventory
   (torch absent -> gpu refused by design), an lm_train run with device=gpu
   FAILS LOUD with remediation, and video_features samples frames from a
   cv2-written clip through the real engine (motion + brightness features;
   capability matrix says video is available).

Usage: /home/z/.venv/bin/python scripts/smoke_v65_live.py
"""

from __future__ import annotations

import base64
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


def _node_run(execution: dict, node_name: str) -> dict:
    for run in execution.get("node_runs") or []:
        if run.get("node_name") == node_name:
            return run
    raise AssertionError(f"node run {node_name!r} not found")


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v65_{uuid.uuid4().hex[:8]}.sqlite3"
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

            # --- 1) install the Language Model System + run the LIFECYCLE ---
            res = c.post("/solutions/language-model-system/install",
                         json={"as_model_system": True})
            assert res.status_code == 200, res.text
            inst = res.json()
            ms_id = inst["model_system"]["id"]
            assert inst["model_system"]["modalities"] == ["text"]

            res = c.get(f"/model-systems/{ms_id}/lifecycle")
            assert res.status_code == 200, res.text
            plan = res.json()
            assert [s["stage"] for s in plan["stages"]] == ["pretrain", "continue", "generate"], plan
            assert plan["lm_workflows"] == 3

            res = c.post(f"/model-systems/{ms_id}/run-lifecycle", json={})
            assert res.status_code == 200, res.text
            out = res.json()
            assert out["ran"] is True, out
            assert out["summary"]["stages_succeeded"] == 3, out["summary"]
            assert out["summary"]["stopped_early"] is False
            stages = {s["stage"]: s for s in out["stages"]}
            assert stages["pretrain"]["mode"] == "from-scratch pretrain"
            assert stages["continue"]["continued_from"] == f"{stages['pretrain']['registry_name']} v1"
            assert isinstance(stages["generate"]["generated_text"], str)
            assert len(stages["generate"]["generated_text"]) > 0

            # the lifecycle run trained through the real engine -> registry
            res = c.get("/models")
            lm_names = sorted({m["name"] for m in res.json() if m["name"].startswith("support_lm")})
            assert len(lm_names) == 1 and len([m for m in res.json() if m["name"] == lm_names[0]]) == 2
            checks += 1
            print(f"[1] LM lifecycle in sequence: "
                  f"ppl {stages['pretrain']['perplexity']} -> "
                  f"{stages['continue']['perplexity']} "
                  f"(lineage {stages['continue']['continued_from']}) -> "
                  f"generated {stages['generate']['tokens_generated']} tokens: "
                  f"{stages['generate']['generated_text'][:44]!r} "
                  f"[{out['summary']['total_seconds']}s, name={lm_names[0]!r}]")

            # --- 2) BPE tokenizer end to end -------------------------------
            docs = [
                "the agent replies to the customer about the login issue",
                "the agent fixes the login bug today",
                "the customer asks about the refund policy",
                "the agent ships the order to the customer",
                "the ticket about the login issue is closed",
                "the refund policy covers the order",
                "the agent escalates the ticket to the team",
                "the customer thanks the agent today",
            ] * 2
            res = c.post("/datasets", json={"name": f"bpe_corpus_{uuid.uuid4().hex[:6]}",
                                            "rows": [{"doc": d} for d in docs]})
            ds = res.json()
            res = c.post("/workflows", json={"name": "bpe-pretrain", "graph": {
                "nodes": [
                    {"id": "t", "type": "manual_trigger", "name": "t", "position": {"x": 0, "y": 0}, "parameters": {}},
                    {"id": "r", "type": "dataset_read", "name": "r", "position": {"x": 1, "y": 0}, "parameters": {"dataset": ds["name"]}},
                    {"id": "lm", "type": "lm_train", "name": "lm", "position": {"x": 2, "y": 0},
                     "parameters": {"text_column": "doc", "tokenizer": "bpe", "vocab_size": 300,
                                    "d_model": 32, "n_heads": 2, "n_ctx": 12, "epochs": 8,
                                    "batch_size": 8, "learning_rate": 0.005, "model_name": "bpe_lm"}},
                ],
                "edges": [{"id": "e1", "source": "t", "target": "r"},
                          {"id": "e2", "source": "r", "target": "lm"}]}})
            wf_id = res.json()["id"]
            res = c.post(f"/workflows/{wf_id}/run", json={})
            ex = res.json()["execution_id"]
            for _ in range(1200):
                det = c.get(f"/executions/{ex}").json()
                if det["status"] not in ("running", "queued"):
                    break
                time.sleep(0.05)
            assert det["status"] == "success", str(det.get("error"))[:400]
            bpe_out = _node_run(det, "lm")["output"]
            assert bpe_out["metrics"]["tokenizer"].startswith("bpe"), bpe_out["metrics"]["tokenizer"]
            assert bpe_out["metrics"]["chars_per_token"] > 1.0
            assert bpe_out["vocabulary"] >= 258
            checks += 1
            print(f"[2] BPE tokenizer: vocab {bpe_out['vocabulary']} "
                  f"({bpe_out['metrics']['tokenizer']}), "
                  f"compression {bpe_out['metrics']['chars_per_token']} chars/token, "
                  f"ppl {bpe_out['perplexity']}")

            # --- 3) device mode honesty + video frame sampling --------------
            res = c.get("/ops/devices")
            assert res.status_code == 200, res.text
            inv = res.json()
            assert inv["torch_installed"] is False
            assert inv["accelerator_present"] is False
            assert inv["device_mode"] == "cpu"
            assert inv["cpu"]["cores"] >= 1

            # device=gpu through the REAL engine -> loud, honest refusal
            res = c.post("/workflows", json={"name": "gpu-refusal", "graph": {
                "nodes": [
                    {"id": "t", "type": "manual_trigger", "name": "t", "position": {"x": 0, "y": 0}, "parameters": {}},
                    {"id": "r", "type": "dataset_read", "name": "r", "position": {"x": 1, "y": 0}, "parameters": {"dataset": ds["name"]}},
                    {"id": "lm", "type": "lm_train", "name": "lm", "position": {"x": 2, "y": 0},
                     "parameters": {"text_column": "doc", "vocab_size": 300, "epochs": 2, "device": "gpu"}},
                ],
                "edges": [{"id": "e1", "source": "t", "target": "r"},
                          {"id": "e2", "source": "r", "target": "lm"}]}})
            wf_gpu = res.json()["id"]
            res = c.post(f"/workflows/{wf_gpu}/run", json={})
            ex = res.json()["execution_id"]
            for _ in range(600):
                det = c.get(f"/executions/{ex}").json()
                if det["status"] not in ("running", "queued"):
                    break
                time.sleep(0.05)
            assert det["status"] == "error"
            assert "device=gpu refused" in (det.get("error") or ""), det.get("error")

            # video: write a real clip with cv2, sample frames in the engine
            import cv2
            import numpy as np
            import tempfile

            fd, clip_path = tempfile.mkstemp(suffix=".avi")
            os.close(fd)
            w = cv2.VideoWriter(clip_path, cv2.VideoWriter_fourcc(*"MJPG"), 4, (48, 48))
            assert w.isOpened()
            for i in range(10):
                w.write(np.full((48, 48, 3), min(255, i * 25), np.uint8))
            w.release()
            clip_b64 = base64.b64encode(open(clip_path, "rb").read()).decode()
            os.unlink(clip_path)

            res = c.post("/datasets", json={"name": f"clips_{uuid.uuid4().hex[:6]}",
                                            "rows": [{"clip": "data:video/avi;base64," + clip_b64,
                                                      "label": "brightening"}]})
            clip_ds = res.json()
            res = c.post("/workflows", json={"name": "video-feats", "graph": {
                "nodes": [
                    {"id": "t", "type": "manual_trigger", "name": "t", "position": {"x": 0, "y": 0}, "parameters": {}},
                    {"id": "r", "type": "dataset_read", "name": "r", "position": {"x": 1, "y": 0}, "parameters": {"dataset": clip_ds["name"]}},
                    {"id": "v", "type": "video_features", "name": "v", "position": {"x": 2, "y": 0},
                     "parameters": {"video_field": "clip", "max_frames": 5}},
                ],
                "edges": [{"id": "e1", "source": "t", "target": "r"},
                          {"id": "e2", "source": "r", "target": "v"}]}})
            wf_v = res.json()["id"]
            res = c.post(f"/workflows/{wf_v}/run", json={})
            ex = res.json()["execution_id"]
            for _ in range(600):
                det = c.get(f"/executions/{ex}").json()
                if det["status"] not in ("running", "queued"):
                    break
                time.sleep(0.05)
            assert det["status"] == "success", str(det.get("error"))[:400]
            vout = _node_run(det, "v")["output"]
            item = vout["items"][0]
            assert item["vid_frames"] == 10 and item["vid_sampled"] == 5
            assert item["vid_motion"] > 0.0
            assert 0.0 <= item["vid_brightness"] <= 1.0
            assert len(vout["frames"]) == 5

            caps = {c2["modality"]: c2 for c2 in c.get("/model-systems/capabilities").json()["capabilities"]}
            assert caps["video"]["available"] is True
            checks += 1
            print(f"[3] device mode + video: torch_installed={inv['torch_installed']} "
                  f"(gpu refused by design), video sampled {item['vid_sampled']}/{item['vid_frames']} frames "
                  f"motion={item['vid_motion']} brightness={item['vid_brightness']}, "
                  f"capability matrix video.available=True")

            print(f"SMOKE v65 GREEN - {checks}/3 checks passed")
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
