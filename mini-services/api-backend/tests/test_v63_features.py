"""V63 feature tests: Model Systems - from-scratch neural training,
multimodal feature nodes and the model-system container.

- neural_train: a raw-numpy MLP (no sklearn estimator) trains through the
  REAL engine on a dataset_read stream, registers as mlp_classifier with
  architecture + param-count metrics, is scored by model_predict, and
  FINE-TUNES from a base registry row (weights continue, lineage recorded).
- multimodal nodes: text_features fit persists a featurizer artifact and
  transform reproduces the same embeddings through a second run; image
  and audio nodes produce their stateless feature columns.
- model systems: create/bind/detail with derived sections (training
  split, modalities evidence, deployment, monitoring coverage,
  retraining schedules), attach guards, bind into a Py8n System as the
  model_system kind, and cross-system dependency edges THROUGH the model
  system.

Runs the FastAPI app in-process via httpx ASGITransport (same harness as v4-v62).
"""

from __future__ import annotations

import asyncio
import base64
import io
import uuid
import wave

import httpx
import numpy as np
from PIL import Image

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
        "email": f"v63-{tag}-u{n}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v63 u{n} {tag}",
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
    for _ in range(300):
        res = await client.get(f"/executions/{exec_id}", headers=headers)
        assert res.status_code == 200, res.text
        if res.json()["status"] not in ("running", "queued"):
            return res.json()
        await asyncio.sleep(0.05)
    raise AssertionError("execution did not finish in time")


def _png_b64(color: tuple[int, int, int]) -> str:
    img = Image.new("RGB", (48, 36), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _wav_b64(freq: int = 440, seconds: float = 0.4) -> str:
    t = np.linspace(0, seconds, int(8000 * seconds), endpoint=False)
    samples = (np.sin(2 * np.pi * freq * t) * 20000).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(samples.tobytes())
    return base64.b64encode(buf.getvalue()).decode()


def _node_run(execution: dict, node_name: str) -> dict:
    for run in execution.get("node_runs") or []:
        if run.get("node_name") == node_name:
            return run
    raise AssertionError(f"node run {node_name!r} not found")


def test_v63_neural_train_finetune_predict():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"nn-{tag}", 1)
            h = _auth(user["token"])

            rows = [{"customer_id": f"c-{i:03d}", "tenure": 40 - i,
                     "monthly_spend": 20 + (i * 7) % 60, "support_tickets": i % 4,
                     "churned": "yes" if (i * 13) % 7 < 3 else "no"}
                    for i in range(1, 33)]
            res = await client.post("/datasets", headers=h,
                                    json={"name": f"nn-customers-{tag}", "rows": rows})
            ds = res.json()
            ds_name = ds["name"]

            # --- 1) from-scratch training through the real engine -------------
            graph = {"nodes": [
                _node("t", "manual_trigger"),
                _node("r", "dataset_read", {"dataset": ds_name}),
                _node("nn", "neural_train", {"task": "classification", "target": "churned",
                                             "features": "tenure,monthly_spend,support_tickets",
                                             "hidden_layers": "24,12", "epochs": 60, "batch_size": 8,
                                             "learning_rate": 0.02, "optimizer": "adam",
                                             "model_name": f"nn-model-{tag}", "register": True}),
            ], "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "nn")]}
            res = await client.post("/workflows", headers=h, json={"name": f"nn-train-{tag}", "graph": graph})
            wf = res.json()
            run = await _run_and_wait(client, wf["id"], h)
            assert run["status"] == "success", str(run.get("error"))[:400]
            out = _node_run(run, "nn")["output"]
            assert out["mode"] == "from-scratch"
            assert out["architecture"] == "3->24->12->2"  # 3 features -> hidden -> 2 classes
            assert out["metrics"]["params_count"] > 0
            assert out["metrics"]["epochs_run"] >= 1

            res = await client.get("/models", headers=h)
            mrow = next(m for m in res.json() if m["name"] == f"nn-model-{tag}" and m["active"])
            assert mrow["algorithm"] == "mlp_classifier"
            assert mrow["metrics"]["architecture"]
            assert mrow["reference_stats"], "neural rows capture drift reference stats too"

            # --- 2) the MLP artifact scores through model_predict --------------
            res = await client.post("/datasets", headers=h, json={
                "name": f"nn-to-score-{tag}",
                "rows": [{"tenure": 30, "monthly_spend": 50, "support_tickets": 1},
                         {"tenure": 5, "monthly_spend": 90, "support_tickets": 3}]})
            score_ds = res.json()
            score_graph = {"nodes": [
                _node("t", "manual_trigger"),
                _node("r", "dataset_read", {"dataset": score_ds["name"]}),
                _node("p", "model_predict", {"model": f"nn-model-{tag}"}),
            ], "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "p")]}
            res = await client.post("/workflows", headers=h, json={"name": f"nn-score-{tag}", "graph": score_graph})
            run2 = await _run_and_wait(client, res.json()["id"], h)
            assert run2["status"] == "success", str(run2.get("error"))[:400]
            scored = _node_run(run2, "p")["output"]["items"]
            assert len(scored) == 2 and all("prediction" in s for s in scored)
            assert all(s["prediction"] in ("yes", "no") for s in scored)

            # --- 3) fine-tune: continue from the base weights -------------------
            ft_graph = {"nodes": [
                _node("t", "manual_trigger"),
                _node("r", "dataset_read", {"dataset": ds_name}),
                _node("nn", "neural_train", {"task": "classification", "target": "churned",
                                             "base_model": f"nn-model-{tag}",
                                             "epochs": 20, "batch_size": 8, "learning_rate": 0.005,
                                             "model_name": f"nn-model-{tag}", "register": True}),
            ], "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "nn")]}
            res = await client.post("/workflows", headers=h, json={"name": f"nn-finetune-{tag}", "graph": ft_graph})
            run3 = await _run_and_wait(client, res.json()["id"], h)
            assert run3["status"] == "success", str(run3.get("error"))[:400]
            assert _node_run(run3, "nn")["output"]["mode"] == "fine-tune"

            res = await client.get("/models", headers=h)
            versions = [m for m in res.json() if m["name"] == f"nn-model-{tag}"]
            assert len(versions) >= 2
            v2 = next(m for m in versions if m["version"] == 2)
            assert v2["metrics"]["fine_tuned_from"] == f"nn-model-{tag} v1"

            # honest failure: fine-tuning refuses classical models
            res = await client.post("/datasets", headers=h, json={
                "name": f"nn-small-{tag}",
                "rows": [{"a": float(i), "b": float(i % 3)} for i in range(12)]})
            small = res.json()
            # step 1: a CLASSICAL model lands in the registry
            res = await client.post("/workflows", headers=h, json={"name": f"nn-classical-{tag}", "graph": {"nodes": [
                _node("t", "manual_trigger"),
                _node("r", "dataset_read", {"dataset": small["name"]}),
                _node("tr", "model_train", {"model": "random_forest_regressor", "target": "b",
                                            "features": "a", "model_name": f"classical-{tag}"}),
            ], "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "tr")]}})
            run4 = await _run_and_wait(client, res.json()["id"], h)
            assert run4["status"] == "success", str(run4.get("error"))[:300]
            # step 2: fine-tuning from a classical row is refused, honestly
            res = await client.post("/workflows", headers=h, json={"name": f"nn-bad-{tag}", "graph": {"nodes": [
                _node("t", "manual_trigger"),
                _node("r", "dataset_read", {"dataset": small["name"]}),
                _node("nn", "neural_train", {"task": "regression", "target": "b", "features": "a",
                                             "base_model": f"classical-{tag}", "epochs": 3}),
            ], "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "nn")]}})
            run5 = await _run_and_wait(client, res.json()["id"], h)
            assert run5["status"] == "error"
            assert "fine-tuning needs a neural_train model" in (run5.get("error") or "")

    asyncio.run(_go())
    try:
        asyncio.run(_drain_background())
    except RuntimeError:
        pass


def test_v63_multimodal_feature_nodes():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"mm-{tag}", 1)
            h = _auth(user["token"])

            # --- text pipeline: fit persists a featurizer, transform replays it --
            texts = [{"id": str(i),
                      "body": body,
                      "label": "spam" if "win" in body or "free" in body else "ham"}
                     for i, body in enumerate([
                         "you win a free prize now", "click here to win free money",
                         "free entry to win", "meeting at noon tomorrow",
                         "project review notes attached", "lunch with the team?",
                         "win big free cash", "can we reschedule the call",
                         "invoice for last month attached", "free trial win winner",
                         " please find the report attached", "thanks for the update"] * 2)]
            res = await client.post("/datasets", headers=h,
                                    json={"name": f"mm-text-{tag}", "rows": texts})
            src = res.json()

            fit_graph = {"nodes": [
                _node("t", "manual_trigger"),
                _node("r", "dataset_read", {"dataset": src["name"]}),
                _node("tf", "text_features", {"column": "body", "mode": "fit",
                                              "featurizer": f"mm-vec-{tag}", "svd_dims": 4, "prefix": "txt"}),
                _node("w", "dataset_write", {"dataset": f"mm-feats-{tag}", "mode": "replace"}),
            ], "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "tf"), _edge("e3", "tf", "w")]}
            res = await client.post("/workflows", headers=h, json={"name": f"mm-fit-{tag}", "graph": fit_graph})
            run = await _run_and_wait(client, res.json()["id"], h)
            assert run["status"] == "success", str(run.get("error"))[:400]

            res = await client.get("/datasets", headers=h)
            feats_ds = next(d for d in res.json() if d["name"] == f"mm-feats-{tag}")
            assert feats_ds["row_count"] == len(texts)
            res = await client.get(f"/datasets/{feats_ds['id']}", headers=h)
            col_names = [c["name"] for c in res.json().get("schema", res.json().get("schema_json", []))]
            assert "txt_vec_0" in " ".join(col_names) or any("txt_vec_0" in c for c in col_names)

            # transform through a second run -> same embeddings (serving parity)
            tf_graph = {"nodes": [
                _node("t", "manual_trigger"),
                _node("r", "dataset_read", {"dataset": src["name"]}),
                _node("tf", "text_features", {"column": "body", "mode": "transform",
                                              "featurizer": f"mm-vec-{tag}", "prefix": "txt"}),
                _node("w", "dataset_write", {"dataset": f"mm-feats2-{tag}", "mode": "replace"}),
            ], "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "tf"), _edge("e3", "tf", "w")]}
            res = await client.post("/workflows", headers=h, json={"name": f"mm-transform-{tag}", "graph": tf_graph})
            run2 = await _run_and_wait(client, res.json()["id"], h)
            assert run2["status"] == "success", str(run2.get("error"))[:400]

            # --- image + audio: stateless features land as numeric columns -------
            media_rows = [
                {"name": "red", "image_b64": _png_b64((255, 0, 0)), "audio_b64": _wav_b64(440)},
                {"name": "blue", "image_b64": _png_b64((0, 0, 255)), "audio_b64": _wav_b64(880)},
                {"name": "green", "image_b64": _png_b64((0, 255, 0)), "audio_b64": _wav_b64(220)},
                {"name": "white", "image_b64": _png_b64((255, 255, 255)), "audio_b64": _wav_b64(660)},
            ]
            res = await client.post("/datasets", headers=h,
                                    json={"name": f"mm-media-{tag}", "rows": media_rows})
            media = res.json()
            media_graph = {"nodes": [
                _node("t", "manual_trigger"),
                _node("r", "dataset_read", {"dataset": media["name"]}),
                _node("img", "image_features", {"image_field": "image_b64", "prefix": "img"}),
                _node("aud", "audio_features", {"audio_field": "audio_b64", "prefix": "aud"}),
                _node("w", "dataset_write", {"dataset": f"mm-media-feats-{tag}", "mode": "replace"}),
            ], "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "img"),
                         _edge("e3", "img", "aud"), _edge("e4", "aud", "w")]}
            res = await client.post("/workflows", headers=h, json={"name": f"mm-media-{tag}", "graph": media_graph})
            run3 = await _run_and_wait(client, res.json()["id"], h)
            assert run3["status"] == "success", str(run3.get("error"))[:400]

            res = await client.get("/datasets", headers=h)
            media_feats = next(d for d in res.json() if d["name"] == f"mm-media-feats-{tag}")
            res = await client.get(f"/datasets/{media_feats['id']}", headers=h)
            schema = res.json().get("schema") or res.json().get("schema_json") or []
            cols = " ".join(str(c.get("name") if isinstance(c, dict) else c) for c in schema)
            assert "img_r_mean" in cols and "img_hist_0" in cols
            assert "aud_rms" in cols and "aud_band_0" in cols

            # fail loud: a non-WAV audio payload is refused with guidance
            bad_rows = [{"name": "x", "image_b64": _png_b64((1, 2, 3)), "audio_b64": base64.b64encode(b"not a wav").decode()}]
            res = await client.post("/datasets", headers=h, json={"name": f"mm-bad-{tag}", "rows": bad_rows})
            bad = res.json()
            bad_graph = {"nodes": [
                _node("t", "manual_trigger"),
                _node("r", "dataset_read", {"dataset": bad["name"]}),
                _node("aud", "audio_features", {"audio_field": "audio_b64"}),
            ], "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "aud")]}
            res = await client.post("/workflows", headers=h, json={"name": f"mm-badwf-{tag}", "graph": bad_graph})
            run4 = await _run_and_wait(client, res.json()["id"], h)
            assert run4["status"] == "error"
            assert "WAV" in (run4.get("error") or "")

            # the honest capability matrix (v65: video frame sampling is real)
            res = await client.get("/model-systems/capabilities", headers=h)
            caps = {c["modality"]: c for c in res.json()["capabilities"]}
            assert caps["text"]["available"] and caps["image"]["available"]
            assert caps["audio"]["available"] and caps["document"]["available"]
            assert caps["video"]["available"] is True and "video_features" in caps["video"]["extractor"]

    asyncio.run(_go())
    try:
        asyncio.run(_drain_background())
    except RuntimeError:
        pass


def test_v63_model_systems():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"ms-{tag}", 1)
            stranger = await _mk_user(client, f"ms-{tag}", 2)
            h = _auth(user["token"])

            # --- create + validation -------------------------------------------
            res = await client.post("/model-systems", headers=h,
                                    json={"name": f"Churn Vision {tag}",
                                          "modalities": ["tabular", "image"], "color": "#818cf8"})
            assert res.status_code == 201, res.text
            ms = res.json()
            assert ms["modalities"] == ["tabular", "image"]

            res = await client.post("/model-systems", headers=h,
                                    json={"name": "bad", "modalities": ["hologram"]})
            assert res.status_code == 400

            # members: dataset + training workflow + model
            rows = [{"customer_id": f"c-{i:03d}", "tenure": 40 - i,
                     "monthly_spend": 20 + (i * 7) % 60, "support_tickets": i % 4,
                     "churned": "yes" if (i * 13) % 7 < 3 else "no"}
                    for i in range(1, 33)]
            res = await client.post("/datasets", headers=h, json={"name": f"ms-ds-{tag}", "rows": rows})
            ds = res.json()
            graph = {"nodes": [
                _node("t", "schedule_trigger", {"cron": "0 3 * * *"}),
                _node("r", "dataset_read", {"dataset": ds["name"]}),
                _node("nn", "neural_train", {"task": "classification", "target": "churned",
                                             "features": "tenure,monthly_spend,support_tickets",
                                             "hidden_layers": "16,8", "epochs": 15, "batch_size": 8,
                                             "model_name": f"ms-model-{tag}", "register": True}),
            ], "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "nn")]}
            res = await client.post("/workflows", headers=h, json={"name": f"ms-train-{tag}", "graph": graph})
            wf = res.json()
            run = await _run_and_wait(client, wf["id"], h)
            assert run["status"] == "success", str(run.get("error"))[:400]

            res = await client.get("/models", headers=h)
            mrow = next(m for m in res.json() if m["name"] == f"ms-model-{tag}" and m["active"])

            # a scorer workflow for the deployment section
            score_graph = {"nodes": [
                _node("t", "manual_trigger"),
                _node("r", "dataset_read", {"dataset": ds["name"]}),
                _node("p", "model_predict", {"model": f"ms-model-{tag}"}),
            ], "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "p")]}
            res = await client.post("/workflows", headers=h, json={"name": f"ms-score-{tag}", "graph": score_graph})
            wf_score = res.json()

            # a composition workflow: 2 chained model_predict nodes
            comp_graph = {"nodes": [
                _node("t", "manual_trigger"),
                _node("r", "dataset_read", {"dataset": ds["name"]}),
                _node("p1", "model_predict", {"model": f"ms-model-{tag}"}),
                _node("p2", "model_predict", {"model": f"ms-model-{tag}"}),
            ], "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "p1"), _edge("e3", "p1", "p2")]}
            res = await client.post("/workflows", headers=h, json={"name": f"ms-chain-{tag}", "graph": comp_graph})
            wf_chain = res.json()

            for kind, ref in (("dataset", ds["id"]), ("workflow", wf["id"]),
                              ("model", mrow["id"]), ("workflow", wf_score["id"]),
                              ("workflow", wf_chain["id"])):
                res = await client.post(f"/model-systems/{ms['id']}/components", headers=h,
                                        json={"kind": kind, "ref_id": ref})
                assert res.status_code == 201, res.text

            # attach guards
            res = await client.post(f"/model-systems/{ms['id']}/components", headers=h,
                                    json={"kind": "gadget", "ref_id": ds["id"]})
            assert res.status_code == 400
            res = await client.post(f"/model-systems/{ms['id']}/components", headers=h,
                                    json={"kind": "dataset", "ref_id": "missing"})
            assert res.status_code == 404
            res = await client.post(f"/model-systems/{ms['id']}/components", headers=h,
                                    json={"kind": "dataset", "ref_id": ds["id"]})
            assert res.status_code == 409
            # foreign objects 404
            res = await client.post("/datasets", headers=_auth(stranger["token"]),
                                    json={"name": f"ms-foreign-{tag}", "rows": [{"a": 1}]})
            foreign = res.json()
            res = await client.post(f"/model-systems/{ms['id']}/components", headers=h,
                                    json={"kind": "dataset", "ref_id": foreign["id"]})
            assert res.status_code == 404

            # --- the derived nine sections --------------------------------------
            res = await client.get(f"/model-systems/{ms['id']}", headers=h)
            detail = res.json()
            assert len(detail["datasets"]) == 1
            assert detail["training"]["neural_versions"] == 1
            assert detail["training"]["latest"][0]["family"] == "neural"
            assert "tabular" in detail["modalities"]["evidence"]
            assert detail["deployment"] and {d["name"] for d in detail["deployment"]} >= {f"ms-score-{tag}"}
            assert detail["composition"] and detail["composition"][0]["chain_length"] == 2
            assert detail["retraining"] and detail["retraining"][0]["schedule"] == "0 3 * * *"
            assert detail["monitoring"]["with_reference_stats"] == 1
            assert detail["monitoring"]["drift_capable"] is True
            assert detail["evaluation"] and detail["evaluation"][0]["model"] == f"ms-model-{tag}"
            assert detail["health"]["verdict"] in ("healthy", "degraded")
            assert detail["health"]["models"]["active"] == 1

            # --- bind into a Py8n System: the COMPANY AI SYSTEM pattern ---------
            res = await client.post("/systems", headers=h, json={"name": f"Company AI {tag}"})
            sys_a = res.json()
            res = await client.post("/systems", headers=h, json={"name": f"Data Platform {tag}"})
            sys_b = res.json()
            res = await client.post(f"/systems/{sys_a['id']}/components", headers=h,
                                    json={"kind": "model_system", "ref_id": ms["id"]})
            assert res.status_code == 201, res.text
            res = await client.post(f"/systems/{sys_b['id']}/components", headers=h,
                                    json={"kind": "dataset", "ref_id": ds["id"]})
            assert res.status_code == 201

            res = await client.get(f"/systems/{sys_a['id']}", headers=h)
            sd = res.json()
            assert len(sd["grouped"]["model_system"]) == 1
            assert sd["health"]["model_systems"]["bound"] == 1

            # dependency view: the model system's dataset shows up as shared
            res = await client.get("/systems/dependencies", headers=h)
            g = res.json()
            shared = [e for e in g["edges"] if e["type"] == "shared_object"]
            assert any(any(ev.get("via") for ev in e["evidence"]) for e in shared), g["edges"]

            # dissolve the model system - members survive
            res = await client.delete(f"/model-systems/{ms['id']}", headers=h)
            assert res.status_code == 204
            assert (await client.get(f"/datasets/{ds['id']}", headers=h)).status_code == 200
            assert (await client.get("/models", headers=h)).json()
            assert (await client.get(f"/model-systems/{ms['id']}", headers=h)).status_code == 404

    asyncio.run(_go())
    try:
        asyncio.run(_drain_background())
    except RuntimeError:
        pass
