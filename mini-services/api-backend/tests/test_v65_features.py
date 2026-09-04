"""V65 feature tests: the LM lifecycle experience, the BPE tokenizer,
the honest GPU/device execution mode, and video frame sampling.

- BPE: byte-level Byte Pair Encoding trained inside lm_train (tokenizer="bpe")
  - lossless decode(encode(text)) for ANY text (256 byte tokens base), merges
    carried through continued pretraining, lm_generate decodes through the
    model's own tokenizer, and vocab_size < 258 fails loud with guidance.
- lifecycle: the marketplace's Language Model System installs AS A MODEL
  SYSTEM; GET /model-systems/{id}/lifecycle derives the stage plan
  (pretrain -> continue -> generate) from the bound graphs, and POST
  run-lifecycle runs the three workflows IN SEQUENCE through the real engine.
- device mode: py8n never fakes GPU compute - device=cpu/auto resolve
  honestly (auto notes the fallback), device=gpu FAILS LOUD with remediation,
  training metrics record device provenance, GET /ops/devices reports the
  inventory.
- video: video_features (OpenCV) samples frames uniformly, derives per-frame
  brightness/contrast/edges + clip motion, the capability matrix flips video
  to available, and a bound workflow proves video modality evidence.

Runs the FastAPI app in-process via httpx ASGITransport (same harness as v4-v64).
"""

from __future__ import annotations

import asyncio
import base64
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


async def _mk_user(client: httpx.AsyncClient, tag: str, n: int) -> dict:
    res = await client.post("/auth/register", json={
        "email": f"v65-{tag}-u{n}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v65 u{n} {tag}",
    })
    assert res.status_code == 201, res.text
    body = res.json()
    return {"token": body["token"], "id": body["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _node(nid: str, ntype: str, params: dict | None = None) -> dict:
    return {"id": nid, "type": ntype, "name": nid, "position": {"x": 0, "y": 0}, "parameters": params or {}}


def _edge(eid: str, source: str, target: str) -> dict:
    return {"id": eid, "source": source, "target": target, "sourceHandle": "main", "targetHandle": "main"}


async def _run_and_wait(client: httpx.AsyncClient, wf_id: str, headers: dict) -> dict:
    res = await client.post(f"/workflows/{wf_id}/run", headers=headers, json={})
    assert res.status_code in (200, 202), res.text
    exec_id = res.json()["execution_id"]
    for _ in range(600):
        res = await client.get(f"/executions/{exec_id}", headers=headers)
        assert res.status_code == 200, res.text
        if res.json()["status"] not in ("running", "queued"):
            return res.json()
        await asyncio.sleep(0.05)
    raise AssertionError("execution did not finish in time")


def _node_run(execution: dict, node_name: str) -> dict:
    for run in execution.get("node_runs") or []:
        if run.get("node_name") == node_name:
            return run
    raise AssertionError(f"node run {node_name!r} not found")


BPE_CORPUS = [
    "the agent replies to the customer about the login issue",
    "the agent fixes the login bug today",
    "the customer asks about the refund policy",
    "the agent ships the order to the customer",
    "the ticket about the login issue is closed",
    "the refund policy covers the order",
    "the agent escalates the ticket to the team",
    "the customer thanks the agent today",
    "the login bug blocks the order today",
    "the team reviews the refund ticket",
    "the agent answers the ticket about the policy",
    "the customer reopens the login ticket",
] * 2


def test_v65_bpe_tokenizer():
    # --- unit level: byte-level BPE is LOSSLESS for any text --------------
    from app.engine.nodes.lm import _BPE

    tok = _BPE.train(BPE_CORPUS, 300)
    assert len(tok.merges) > 0, "the trainer must learn merges on a repetitive corpus"
    assert len(tok.vocab) == 258 + len(tok.merges)
    for text in ("the agent replies", "Zebra! 42 ünïcode 中文", "  spaces  ", ""):
        assert tok.decode(tok.encode(text)) == text, "decode(encode(x)) must equal x for any bytes"
    restored = _BPE.from_state(tok.state())
    assert restored.decode(restored.encode("the refund policy")) == "the refund policy"

    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"bpe-{tag}", 1)
            h = _auth(user["token"])

            res = await client.post("/datasets", headers=h,
                                    json={"name": f"bpe-corpus-{tag}",
                                          "rows": [{"doc": d} for d in BPE_CORPUS]})
            corpus = res.json()
            graph = {"nodes": [
                _node("t", "manual_trigger"),
                _node("r", "dataset_read", {"dataset": corpus["name"]}),
                _node("lm", "lm_train", {"text_column": "doc", "tokenizer": "bpe",
                                         "vocab_size": 300, "d_model": 32, "n_heads": 2,
                                         "n_ctx": 12, "epochs": 10, "batch_size": 8,
                                         "learning_rate": 0.005, "model_name": f"bpe-lm-{tag}"}),
            ], "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "lm")]}
            res = await client.post("/workflows", headers=h, json={"name": f"bpe-pretrain-{tag}", "graph": graph})
            run = await _run_and_wait(client, res.json()["id"], h)
            assert run["status"] == "success", str(run.get("error"))[:400]
            out = _node_run(run, "lm")["output"]
            assert out["metrics"]["tokenizer"].startswith("bpe"), out["metrics"]["tokenizer"]
            assert out["metrics"]["chars_per_token"] > 1.0, "merges must compress below 1 char/token"
            assert out["vocabulary"] >= 258
            assert out["perplexity"] >= 1.0
            assert out["metrics"]["device"] == "cpu"

            # --- continued pretraining carries the BPE tokenizer -------------
            res = await client.post("/datasets", headers=h,
                                    json={"name": f"bpe-legal-{tag}",
                                          "rows": [{"doc": d} for d in BPE_CORPUS[:8] * 2]})
            legal = res.json()
            graph2 = {"nodes": [
                _node("t", "manual_trigger"),
                _node("r", "dataset_read", {"dataset": legal["name"]}),
                _node("lm", "lm_train", {"text_column": "doc", "base_model": f"bpe-lm-{tag}",
                                         "epochs": 8, "batch_size": 8, "learning_rate": 0.003,
                                         "model_name": f"bpe-lm-{tag}"}),
            ], "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "lm")]}
            res = await client.post("/workflows", headers=h, json={"name": f"bpe-continue-{tag}", "graph": graph2})
            run2 = await _run_and_wait(client, res.json()["id"], h)
            assert run2["status"] == "success", str(run2.get("error"))[:400]
            out2 = _node_run(run2, "lm")["output"]
            assert out2["mode"] == "continued pretrain"
            assert out2["vocabulary"] == out["vocabulary"], "the fitted BPE vocab must carry over"

            # --- generation decodes through the model's own BPE ---------------
            res = await client.post("/workflows", headers=h, json={"name": f"bpe-generate-{tag}", "graph": {
                "nodes": [
                    _node("t", "manual_trigger", {"payload": {"prompt": "the agent"}}),
                    _node("g", "lm_generate", {"model": f"bpe-lm-{tag}",
                                               "prompt": "{{ nodes.t.output.payload.prompt }}",
                                               "max_tokens": 6, "temperature": 0.7, "top_k": 20}),
                ],
                "edges": [_edge("e1", "t", "g")]}})
            run3 = await _run_and_wait(client, res.json()["id"], h)
            assert run3["status"] == "success", str(run3.get("error"))[:400]
            gen = _node_run(run3, "g")["output"]
            assert gen["tokenizer"] == "bpe"
            assert isinstance(gen["text"], str) and len(gen["text"]) > 0

            # --- honest failure: BPE vocab under the byte floor ---------------
            graph3 = {"nodes": [
                _node("t", "manual_trigger"),
                _node("r", "dataset_read", {"dataset": corpus["name"]}),
                _node("lm", "lm_train", {"text_column": "doc", "tokenizer": "bpe",
                                         "vocab_size": 200, "epochs": 2}),
            ], "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "lm")]}
            res = await client.post("/workflows", headers=h, json={"name": f"bpe-small-{tag}", "graph": graph3})
            run4 = await _run_and_wait(client, res.json()["id"], h)
            assert run4["status"] == "error"
            assert "vocab_size must be >= 258" in (run4.get("error") or "")

    asyncio.run(_go())
    try:
        asyncio.run(_drain_background())
    except RuntimeError:
        pass


def test_v65_lm_lifecycle_run_in_sequence():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user_a = await _mk_user(client, f"lc-{tag}", 1)
            user_b = await _mk_user(client, f"lc-{tag}", 2)
            ha = _auth(user_a["token"])
            hb = _auth(user_b["token"])

            # --- install the Language Model System from the marketplace ------
            res = await client.post("/solutions/language-model-system/install", headers=ha,
                                    json={"as_model_system": True})
            assert res.status_code == 200, res.text
            inst = res.json()
            ms_id = inst["model_system"]["id"]
            wf_by_name = {w["name"]: w for w in inst["created_workflows"]}
            assert {"Pretrain Language Model", "Continue Pretraining",
                    "Generate With Language Model"} <= set(wf_by_name)

            # --- the DERIVED lifecycle plan (nothing stored) ------------------
            res = await client.get(f"/model-systems/{ms_id}/lifecycle", headers=ha)
            assert res.status_code == 200, res.text
            plan = res.json()
            assert plan["lm_workflows"] == 3
            assert [s["stage"] for s in plan["stages"]] == ["pretrain", "continue", "generate"]
            assert [s["workflow_name"] for s in plan["stages"]] == [
                "Pretrain Language Model", "Continue Pretraining", "Generate With Language Model"]
            assert plan["sequence"].startswith("pretrain:")
            # install docs live in a dataset - bound datasets are never lifecycle stages
            assert all("lm_corpus" not in s["workflow_name"] for s in plan["stages"])

            # the detail cockpit carries the same derived plan
            res = await client.get(f"/model-systems/{ms_id}", headers=ha)
            assert res.json()["lifecycle"]["lm_workflows"] == 3

            # foreign eyes see nothing (fail closed)
            res = await client.get(f"/model-systems/{ms_id}/lifecycle", headers=hb)
            assert res.status_code == 404

            # --- RUN THE FULL LM LIFECYCLE IN SEQUENCE ------------------------
            res = await client.post(f"/model-systems/{ms_id}/run-lifecycle", headers=ha, json={})
            assert res.status_code == 200, res.text
            out = res.json()
            assert out["ran"] is True
            assert out["summary"]["stages_total"] == 3
            assert out["summary"]["stages_succeeded"] == 3
            assert out["summary"]["stopped_early"] is False
            statuses = [s["status"] for s in out["stages"]]
            assert statuses == ["success", "success", "success"]
            stages = {s["stage"]: s for s in out["stages"]}
            assert stages["pretrain"]["mode"] == "from-scratch pretrain"
            assert stages["pretrain"]["perplexity"] >= 1.0
            assert stages["continue"]["mode"] == "continued pretrain"
            # lineage is relational: the continue stage continues the name the
            # PRETRAIN stage registered (multi-tenant installs may suffix it)
            assert stages["continue"]["continued_from"] == f"{stages['pretrain']['registry_name']} v1"
            assert stages["continue"]["perplexity"] >= 1.0
            assert isinstance(stages["generate"]["generated_text"], str)
            assert len(stages["generate"]["generated_text"]) > 0
            assert stages["generate"]["tokens_generated"] >= 1
            assert len(out["summary"]["perplexity_chain"]) == 2
            assert out["summary"]["generated_text"] == stages["generate"]["generated_text"]
            # executions really happened, in order
            assert stages["continue"]["execution_id"] != stages["pretrain"]["execution_id"]

            # the lm_samples ledger got its append from the generate stage
            # (name may be suffixed when earlier installs made the base name taken)
            res = await client.get("/datasets", headers=ha)
            samples = next(d for d in res.json() if d["name"].startswith("lm_samples"))
            assert samples["row_count"] >= 1

            # --- a model system WITHOUT LM workflows honestly refuses --------
            res = await client.post("/model-systems", headers=ha, json={"name": f"Empty {tag}"})
            empty_id = res.json()["id"]
            res = await client.post(f"/model-systems/{empty_id}/run-lifecycle", headers=ha, json={})
            assert res.status_code == 200
            body = res.json()
            assert body["ran"] is False
            assert "no LM lifecycle stages" in body["note"]

    asyncio.run(_go())
    try:
        asyncio.run(_drain_background())
    except RuntimeError:
        pass


def test_v65_device_mode():
    # --- unit level: the resolution matrix is honest -----------------------
    from app.services.devices import resolve_device

    ok = resolve_device("cpu")
    assert ok == {"requested": "cpu", "resolved": "cpu", "backend": "numpy",
                  "usable": True, "note": ""}
    auto = resolve_device("auto")
    assert auto["resolved"] == "cpu" and auto["usable"] is True and auto["note"]
    try:
        resolve_device("gpu")
        raise AssertionError("gpu must be refused in this torch-less environment")
    except ValueError as exc:
        assert "device=gpu refused" in str(exc)
        assert "torch is not installed" in str(exc)
    try:
        resolve_device("tpu")
        raise AssertionError("unknown device must be refused")
    except ValueError as exc:
        assert "unknown device" in str(exc)

    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"dev-{tag}", 1)
            h = _auth(user["token"])

            # --- the inventory endpoint reports the truth ---------------------
            res = await client.get("/ops/devices", headers=h)
            assert res.status_code == 200, res.text
            inv = res.json()
            assert inv["device_mode"] == "cpu"
            assert inv["torch_installed"] is False
            assert inv["accelerator_present"] is False
            assert inv["cpu"]["cores"] >= 1
            assert inv["compute_backend"] == "numpy (cpu)"
            assert any("torch is not installed" in n for n in inv["notes"])

            # --- engine E2E: default device lands in the metrics --------------
            res = await client.post("/datasets", headers=h,
                                    json={"name": f"dev-corpus-{tag}",
                                          "rows": [{"doc": d} for d in BPE_CORPUS]})
            corpus = res.json()
            graph = {"nodes": [
                _node("t", "manual_trigger"),
                _node("r", "dataset_read", {"dataset": corpus["name"]}),
                _node("lm", "lm_train", {"text_column": "doc", "vocab_size": 300,
                                         "d_model": 32, "n_heads": 2, "n_ctx": 12,
                                         "epochs": 3, "batch_size": 8, "learning_rate": 0.005}),
            ], "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "lm")]}
            res = await client.post("/workflows", headers=h, json={"name": f"dev-lm-{tag}", "graph": graph})
            run = await _run_and_wait(client, res.json()["id"], h)
            assert run["status"] == "success", str(run.get("error"))[:400]
            out = _node_run(run, "lm")["output"]
            assert out["metrics"]["device"] == "cpu"
            assert out["metrics"]["device_backend"] == "numpy"

            # --- engine E2E: device=gpu FAILS LOUD with remediation -----------
            graph2 = {"nodes": [
                _node("t", "manual_trigger"),
                _node("r", "dataset_read", {"dataset": corpus["name"]}),
                _node("lm", "lm_train", {"text_column": "doc", "vocab_size": 300,
                                         "epochs": 2, "device": "gpu"}),
            ], "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "lm")]}
            res = await client.post("/workflows", headers=h, json={"name": f"dev-lm-gpu-{tag}", "graph": graph2})
            run2 = await _run_and_wait(client, res.json()["id"], h)
            assert run2["status"] == "error"
            assert "device=gpu refused" in (run2.get("error") or "")
            assert "torch is not installed" in (run2.get("error") or "")

    asyncio.run(_go())
    try:
        asyncio.run(_drain_background())
    except RuntimeError:
        pass


def _make_clip(step: int, base: int = 0) -> str:
    import cv2
    import numpy as np
    import tempfile
    import os

    fd, path = tempfile.mkstemp(suffix=".avi")
    os.close(fd)
    w = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"MJPG"), 4, (48, 48))
    assert w.isOpened()
    for i in range(10):
        val = min(255, max(0, base + i * step))
        w.write(np.full((48, 48, 3), val, np.uint8))
    w.release()
    raw = open(path, "rb").read()
    os.unlink(path)
    return base64.b64encode(raw).decode()


def test_v65_video_features():
    tag = uuid.uuid4().hex[:8]
    bright = _make_clip(25)          # 0 -> 225 rising
    dark = _make_clip(-10, base=40)  # 40 -> 0 falling, dim throughout

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"vid-{tag}", 1)
            h = _auth(user["token"])

            # --- two clips through the REAL engine ----------------------------
            res = await client.post("/datasets", headers=h, json={"name": f"clips-{tag}", "rows": [
                {"clip": "data:video/avi;base64," + bright, "label": "brightening"},
                {"clip": "data:video/avi;base64," + dark, "label": "darkening"},
            ]})
            clips = res.json()
            graph = {"nodes": [
                _node("t", "manual_trigger"),
                _node("r", "dataset_read", {"dataset": clips["name"]}),
                _node("v", "video_features", {"video_field": "clip", "max_frames": 5}),
            ], "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "v")]}
            res = await client.post("/workflows", headers=h, json={"name": f"vid-feats-{tag}", "graph": graph})
            run = await _run_and_wait(client, res.json()["id"], h)
            assert run["status"] == "success", str(run.get("error"))[:400]
            out = _node_run(run, "v")["output"]
            assert out["rows_in"] == 2
            items = out["items"]
            assert len(items) == 2
            for it in items:
                assert it["vid_frames"] == 10
                assert it["vid_sampled"] == 5
                assert it["vid_duration_s"] == 2.5
                assert it["vid_fps"] == 4.0
                assert 0.0 <= it["vid_brightness"] <= 1.0
                assert it["vid_motion"] >= 0.0
            by_label = {it["label"]: it for it in items}
            assert by_label["brightening"]["vid_brightness"] > by_label["darkening"]["vid_brightness"]
            frames = out["frames"]
            assert len(frames) == 10  # 5 per clip
            assert frames[0]["frame_index"] == 0 and frames[0]["timestamp_s"] == 0.0
            # the brightening clip's sampled frames rise across time (0 -> 225/255)
            assert frames[4]["brightness"] > frames[0]["brightness"]
            assert frames[4]["brightness"] > 0.8
            assert all(set(f) >= {"row", "frame_index", "timestamp_s", "brightness",
                                  "contrast", "edge_density", "motion_vs_prev"} for f in frames)

            # --- garbage bytes fail loud with guidance ------------------------
            # (the graph also becomes the video-modality evidence fixture below)
            res = await client.post("/datasets", headers=h, json={"name": f"badclips-{tag}", "rows": [
                {"clip": base64.b64encode(b"not a video").decode()}]})
            bad_ds = res.json()
            bad_run_id = (await client.post("/workflows", headers=h, json={"name": f"vid-bad-{tag}", "graph": {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("r", "dataset_read", {"dataset": bad_ds["name"]}),
                    _node("v", "video_features", {"video_field": "clip"}),
                ],
                "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "v")]}})).json()["id"]
            run3 = await _run_and_wait(client, bad_run_id, h)
            assert run3["status"] == "error"
            assert "cannot decode video" in (run3.get("error") or "")

            # --- the capability matrix flips video to available ----------------
            res = await client.get("/model-systems/capabilities", headers=h)
            caps = {c["modality"]: c for c in res.json()["capabilities"]}
            assert caps["video"]["available"] is True
            assert "video_features" in caps["video"]["extractor"]

            # --- a bound workflow proves video modality evidence ---------------
            res = await client.post("/model-systems", headers=h,
                                    json={"name": f"Video Vision {tag}", "modalities": ["video"]})
            ms = res.json()
            res = await client.post(f"/model-systems/{ms['id']}/components", headers=h,
                                    json={"kind": "workflow", "ref_id": bad_run_id})
            assert res.status_code == 201
            res = await client.get(f"/model-systems/{ms['id']}", headers=h)
            detail = res.json()
            assert "video" in detail["modalities"]["evidence"]
            assert caps["video"]["available"] is True

    asyncio.run(_go())
    try:
        asyncio.run(_drain_background())
    except RuntimeError:
        pass
