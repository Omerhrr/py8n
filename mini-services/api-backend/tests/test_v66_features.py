"""V66 feature tests: the torch training backend, multimodal LM fine-tuning
(condition-prefix adapters), and LM drift monitoring.

- torch backend: neural_train and lm_train run on the torch mirror
  (device=torch, CUDA/MPS when present - torch-CPU here) with state-format
  parity, so a numpy-pretrained LM CONTINUES on torch and a torch-trained
  artifact SERVES through numpy (model_predict) with no conversion.
- multimodal fine-tuning: condition_columns attach a condition-prefix
  adapter - from scratch, or on a text-only base (backbone carries over,
  fresh adapter, lineage kept); lm_generate demands the condition vector
  and refuses wrong dims; generation without one fails loud.
- LM drift: lm_train registers a held-out loss-distribution as reference
  stats; drift_check computes the PSI of a new corpus's loss distribution
  against it (stable corpus -> no drift; alien corpus -> drift, and
  on_drift=error fails the run); model systems' monitoring coverage counts
  LM rows at last.

Torch tests skip gracefully when torch is not installed (optional dep).
Runs the FastAPI app in-process via httpx ASGITransport (same harness as v4-v65).
"""

from __future__ import annotations

import asyncio
import importlib.util
import uuid

import httpx

from app.main import app
from app.services import executor as executor_mod

API = "http://testserver/api/v1"

TORCH_HERE = importlib.util.find_spec("torch") is not None


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _drain_background() -> None:
    tasks = [t for t in executor_mod._background_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _mk_user(client: httpx.AsyncClient, tag: str, n: int) -> dict:
    res = await client.post("/auth/register", json={
        "email": f"v66-{tag}-u{n}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v66 u{n} {tag}",
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
    for _ in range(1200):
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


async def _make_lm(client: httpx.AsyncClient, h: dict, tag: str, name: str, *,
                   device: str = "cpu", epochs: int = 8, d_model: int = 24,
                   vocab_size: int = 120, register: bool = True) -> dict:
    res = await client.post("/datasets", headers=h,
                            json={"name": f"lm-{name}-{tag}", "rows": [{"doc": d} for d in CORPUS]})
    ds = res.json()
    graph = {"nodes": [
        _node("t", "manual_trigger"),
        _node("r", "dataset_read", {"dataset": ds["name"]}),
        _node("lm", "lm_train", {"text_column": "doc", "vocab_size": vocab_size,
                                 "d_model": d_model, "n_heads": 2, "n_ctx": 12,
                                 "epochs": epochs, "batch_size": 8, "learning_rate": 0.005,
                                 "device": device, "model_name": name, "register": register}),
    ], "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "lm")]}
    res = await client.post("/workflows", headers=h, json={"name": f"wf-{name}-{tag}", "graph": graph})
    run = await _run_and_wait(client, res.json()["id"], h)
    assert run["status"] == "success", str(run.get("error"))[:400]
    return _node_run(run, "lm")["output"]


def test_v66_torch_backend():
    if not TORCH_HERE:
        raise AssertionError("torch expected in this environment (optional dep installed)")

    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"torch-{tag}", 1)
            h = _auth(user["token"])

            # --- 1) neural_train on the torch backend -------------------------
            res = await client.post("/datasets", headers=h, json={"name": f"tab-{tag}", "rows": [
                {"a": float(i), "b": float(i % 3), "y": 1 if i % 2 else 0} for i in range(14)]})
            ds = res.json()
            graph = {"nodes": [
                _node("t", "manual_trigger"),
                _node("r", "dataset_read", {"dataset": ds["name"]}),
                _node("nt", "neural_train", {"task": "classification", "target": "y", "features": "a,b",
                                             "hidden_layers": "12", "epochs": 30, "batch_size": 8,
                                             "learning_rate": 0.03, "device": "torch",
                                             "model_name": f"torch-net-{tag}"}),
            ], "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "nt")]}
            res = await client.post("/workflows", headers=h, json={"name": f"torch-net-{tag}", "graph": graph})
            run = await _run_and_wait(client, res.json()["id"], h)
            assert run["status"] == "success", str(run.get("error"))[:400]
            out = _node_run(run, "nt")["output"]
            assert out["metrics"]["device_backend"] == "torch"
            assert out["metrics"]["device"] == "cpu"  # torch-CPU (no accelerator here)
            assert out["metrics"]["accuracy"] >= 0.0
            assert "torch" in (out["metrics"].get("device_note") or "")

            # torch-trained artifact SERVES through the numpy core
            res = await client.post("/workflows", headers=h, json={"name": f"serve-{tag}", "graph": {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("r", "dataset_read", {"dataset": ds["name"]}),
                    _node("mp", "model_predict", {"model": f"torch-net-{tag}", "features": "a,b"}),
                ],
                "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "mp")]}})
            run = await _run_and_wait(client, res.json()["id"], h)
            assert run["status"] == "success", str(run.get("error"))[:300]
            preds = _node_run(run, "mp")["output"]["items"]
            assert all(p["prediction"] in (0, 1) for p in preds)

            # --- 2) lm_train on the torch backend + torch generation ----------
            out = await _make_lm(client, h, tag, f"torch-lm-{tag}", device="torch", epochs=8)
            assert out["metrics"]["device_backend"] == "torch"
            assert out["metrics"]["device"] == "cpu"
            assert out["perplexity"] >= 1.0

            res = await client.post("/workflows", headers=h, json={"name": f"torch-gen-{tag}", "graph": {
                "nodes": [
                    _node("t", "manual_trigger", {"payload": {"prompt": "the agent"}}),
                    _node("g", "lm_generate", {"model": f"torch-lm-{tag}",
                                               "prompt": "{{ nodes.t.output.payload.prompt }}",
                                               "max_tokens": 5, "temperature": 0.7, "top_k": 15,
                                               "device": "torch"}),
                ],
                "edges": [_edge("e1", "t", "g")]}})
            run = await _run_and_wait(client, res.json()["id"], h)
            assert run["status"] == "success", str(run.get("error"))[:400]
            gen = _node_run(run, "g")["output"]
            assert gen["device_backend"] == "torch"
            assert len(gen["text"]) > 0

            # --- 3) CROSS-BACKEND: numpy pretrain -> torch continue -----------
            out_np = await _make_lm(client, h, tag, f"np-lm-{tag}", device="cpu", epochs=8,
                                    vocab_size=120, d_model=24)
            assert out_np["metrics"]["device_backend"] == "numpy"
            res = await client.post("/datasets", headers=h,
                                    json={"name": f"lm2-{tag}", "rows": [{"doc": d} for d in CORPUS]})
            ds2 = res.json()
            graph2 = {"nodes": [
                _node("t", "manual_trigger"),
                _node("r", "dataset_read", {"dataset": ds2["name"]}),
                _node("lm", "lm_train", {"text_column": "doc", "base_model": f"np-lm-{tag}",
                                         "epochs": 6, "batch_size": 8, "learning_rate": 0.003,
                                         "device": "torch", "model_name": f"np-lm-{tag}"}),
            ], "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "lm")]}
            res = await client.post("/workflows", headers=h, json={"name": f"x-backend-{tag}", "graph": graph2})
            run2 = await _run_and_wait(client, res.json()["id"], h)
            assert run2["status"] == "success", str(run2.get("error"))[:400]
            out2 = _node_run(run2, "lm")["output"]
            assert out2["mode"] == "continued pretrain"
            assert out2["metrics"]["continued_pretrained_from"] == f"np-lm-{tag} v1"
            assert out2["metrics"]["device_backend"] == "torch"
            assert out2["vocabulary"] == out_np["vocabulary"], "tokenizer carried across backends"

    asyncio.run(_go())
    try:
        asyncio.run(_drain_background())
    except RuntimeError:
        pass


def test_v66_multimodal_lm_finetuning():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"mm-{tag}", 1)
            h = _auth(user["token"])

            rows = [{"doc": CORPUS[i % len(CORPUS)],
                     "img_brightness": round(0.2 + 0.05 * (i % 8), 3),
                     "img_edges": round(0.1 + 0.03 * (i % 5), 3)}
                    for i in range(20)]
            res = await client.post("/datasets", headers=h, json={"name": f"mm-{tag}", "rows": rows})
            ds = res.json()

            # --- 1) from-scratch CONDITIONED LM -------------------------------
            graph = {"nodes": [
                _node("t", "manual_trigger"),
                _node("r", "dataset_read", {"dataset": ds["name"]}),
                _node("lm", "lm_train", {"text_column": "doc", "condition_columns": "img_brightness,img_edges",
                                         "vocab_size": 120, "d_model": 24, "n_heads": 2, "n_ctx": 12,
                                         "epochs": 8, "batch_size": 8, "learning_rate": 0.005,
                                         "model_name": f"mm-lm-{tag}"}),
            ], "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "lm")]}
            res = await client.post("/workflows", headers=h, json={"name": f"mm-pre-{tag}", "graph": graph})
            run = await _run_and_wait(client, res.json()["id"], h)
            assert run["status"] == "success", str(run.get("error"))[:400]
            out = _node_run(run, "lm")["output"]
            assert out["metrics"]["multimodal"] is True
            assert out["metrics"]["condition_dim"] == 2
            assert out["metrics"]["condition_columns"] == ["img_brightness", "img_edges"]

            # --- 2) generation demands the condition vector -------------------
            gen_graph = {"nodes": [
                _node("t", "manual_trigger", {"payload": {"prompt": "the agent",
                                                          "c1": 0.4, "c2": 0.25}}),
                _node("g", "lm_generate", {"model": f"mm-lm-{tag}",
                                           "prompt": "{{ nodes.t.output.payload.prompt }}",
                                           "condition": "{{ [nodes.t.output.payload.c1, nodes.t.output.payload.c2] | join(',') }}",
                                           "max_tokens": 5, "temperature": 0.7, "top_k": 15}),
            ], "edges": [_edge("e1", "t", "g")]}
            res = await client.post("/workflows", headers=h, json={"name": f"mm-gen-{tag}", "graph": gen_graph})
            wf_gen = res.json()["id"]
            run = await _run_and_wait(client, wf_gen, h)
            assert run["status"] == "success", str(run.get("error"))[:400]
            gen = _node_run(run, "g")["output"]
            assert gen["conditioned"] is True
            assert gen["condition"] == [0.4, 0.25]
            assert len(gen["text"]) > 0

            # without the condition -> honest refusal
            res = await client.post("/workflows", headers=h, json={"name": f"mm-gen0-{tag}", "graph": {
                "nodes": [_node("t", "manual_trigger"),
                          _node("g", "lm_generate", {"model": f"mm-lm-{tag}", "prompt": "the agent"})],
                "edges": [_edge("e1", "t", "g")]}})
            run0 = await _run_and_wait(client, res.json()["id"], h)
            assert run0["status"] == "error"
            assert "CONDITIONED" in (run0.get("error") or "")

            # wrong dimension -> honest refusal
            res = await client.post("/workflows", headers=h, json={"name": f"mm-gen2-{tag}", "graph": {
                "nodes": [_node("t", "manual_trigger"),
                          _node("g", "lm_generate", {"model": f"mm-lm-{tag}", "prompt": "the agent",
                                                     "condition": "0.4, 0.25, 0.9"})],
                "edges": [_edge("e1", "t", "g")]}})
            run2 = await _run_and_wait(client, res.json()["id"], h)
            assert run2["status"] == "error"
            assert "condition dimension mismatch" in (run2.get("error") or "")

            # --- 3) MULTIMODAL FINE-TUNING: text-only base gains the adapter --
            out_text = await _make_lm(client, h, tag, f"txt-lm-{tag}", epochs=6)
            assert out_text["metrics"]["multimodal"] is False
            res = await client.post("/workflows", headers=h, json={"name": f"mm-ft-{tag}", "graph": {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("r", "dataset_read", {"dataset": ds["name"]}),
                    _node("lm", "lm_train", {"text_column": "doc", "base_model": f"txt-lm-{tag}",
                                             "condition_columns": "img_brightness,img_edges",
                                             "epochs": 6, "batch_size": 8, "learning_rate": 0.003,
                                             "model_name": f"txt-lm-{tag}"}),
                ],
                "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "lm")]}})
            run3 = await _run_and_wait(client, res.json()["id"], h)
            assert run3["status"] == "success", str(run3.get("error"))[:400]
            out3 = _node_run(run3, "lm")["output"]
            assert out3["mode"] == "continued pretrain"
            assert out3["metrics"]["continued_pretrained_from"] == f"txt-lm-{tag} v1"
            assert out3["metrics"]["multimodal_adapter_added"] is True
            assert out3["metrics"]["condition_dim"] == 2
            assert out3["vocabulary"] == out_text["vocabulary"], "tokenizer carried over"

            # --- 4) honest refusals on the base -------------------------------
            # conditioned base without condition_columns
            graph4 = {"nodes": [
                _node("t", "manual_trigger"),
                _node("r", "dataset_read", {"dataset": ds["name"]}),
                _node("lm", "lm_train", {"text_column": "doc", "base_model": f"mm-lm-{tag}",
                                         "epochs": 3, "model_name": f"mm-lm-{tag}"}),
            ], "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "lm")]}
            res = await client.post("/workflows", headers=h, json={"name": f"mm-bad1-{tag}", "graph": graph4})
            run4 = await _run_and_wait(client, res.json()["id"], h)
            assert run4["status"] == "error"
            assert "CONDITIONED" in (run4.get("error") or "")

            # dimension mismatch on the conditioned base
            res = await client.post("/workflows", headers=h, json={"name": f"mm-bad2-{tag}", "graph": {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("r", "dataset_read", {"dataset": ds["name"]}),
                    _node("lm", "lm_train", {"text_column": "doc", "base_model": f"mm-lm-{tag}",
                                             "condition_columns": "img_brightness",
                                             "epochs": 3, "model_name": f"mm-lm-{tag}"}),
                ],
                "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "lm")]}})
            run5 = await _run_and_wait(client, res.json()["id"], h)
            assert run5["status"] == "error"
            assert "condition dimension mismatch" in (run5.get("error") or "")

            # missing/non-numeric condition values fail loud per row
            res = await client.post("/datasets", headers=h, json={"name": f"mm-bad3-{tag}", "rows": [
                {"doc": CORPUS[i % len(CORPUS)], "img_brightness": 0.3,
                 "img_edges": "not-a-number"} for i in range(16)]})
            ds_bad = res.json()
            res = await client.post("/workflows", headers=h, json={"name": f"mm-bad4-{tag}", "graph": {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("r", "dataset_read", {"dataset": ds_bad["name"]}),
                    _node("lm", "lm_train", {"text_column": "doc",
                                             "condition_columns": "img_brightness,img_edges",
                                             "epochs": 2, "register": False}),
                ],
                "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "lm")]}})
            run6 = await _run_and_wait(client, res.json()["id"], h)
            assert run6["status"] == "error"
            assert "not numeric" in (run6.get("error") or "")

    asyncio.run(_go())
    try:
        asyncio.run(_drain_background())
    except RuntimeError:
        pass


def test_v66_lm_drift_monitoring():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"dr-{tag}", 1)
            h = _auth(user["token"])

            # --- 1) lm_train REGISTERS loss-distribution reference stats ------
            out = await _make_lm(client, h, tag, f"drift-lm-{tag}", epochs=8, vocab_size=120)
            res = await client.get("/models", headers=h)
            row = next(m for m in res.json() if m["name"] == f"drift-lm-{tag}" and m["active"])
            ref = row.get("reference_stats") or {}
            assert ref.get("kind") == "lm_loss"
            assert len(ref["histogram"]) == 10
            assert abs(sum(ref["histogram"]) - 1.0) < 0.01
            assert ref["ppl"] == out["perplexity"]
            assert ref["window_count"] >= 1

            # --- 2) drift_check on the SAME corpus -> stable -------------------
            res = await client.post("/datasets", headers=h,
                                    json={"name": f"same-{tag}", "rows": [{"doc": d} for d in CORPUS]})
            same = res.json()
            res = await client.post("/workflows", headers=h, json={"name": f"dr-same-{tag}", "graph": {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("r", "dataset_read", {"dataset": same["name"]}),
                    _node("dc", "drift_check", {"model": f"drift-lm-{tag}", "text_column": "doc",
                                                "threshold": 0.25}),
                ],
                "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "dc")]}})
            run = await _run_and_wait(client, res.json()["id"], h)
            assert run["status"] == "success", str(run.get("error"))[:400]
            rep = _node_run(run, "dc")["output"]["report"]
            assert rep["signal"] == "lm_loss_psi"
            assert rep["drift_detected"] is False
            assert rep["overall_psi"] < 0.25
            assert rep["window_count"] >= 1

            # --- 3) drift_check on an ALIEN corpus -> drift; error mode fails --
            res = await client.post("/datasets", headers=h,
                                    json={"name": f"alien-{tag}", "rows": [{"doc": d} for d in ALIEN_CORPUS]})
            alien = res.json()
            res = await client.post("/workflows", headers=h, json={"name": f"dr-alien-{tag}", "graph": {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("r", "dataset_read", {"dataset": alien["name"]}),
                    _node("dc", "drift_check", {"model": f"drift-lm-{tag}", "text_column": "doc",
                                                "threshold": 0.25, "on_drift": "error"}),
                ],
                "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "dc")]}})
            run2 = await _run_and_wait(client, res.json()["id"], h)
            assert run2["status"] == "error"
            assert "Drift detected" in (run2.get("error") or "")
            assert "ce_loss_distribution" in (run2.get("error") or "")

            # --- 4) model systems monitoring now counts LM rows ----------------
            res = await client.post("/model-systems", headers=h,
                                    json={"name": f"Drift Vision {tag}", "modalities": ["text"]})
            ms = res.json()
            res = await client.post(f"/model-systems/{ms['id']}/components", headers=h,
                                    json={"kind": "model", "ref_id": row["id"]})
            assert res.status_code == 201
            res = await client.get(f"/model-systems/{ms['id']}", headers=h)
            detail = res.json()
            assert detail["monitoring"]["with_reference_stats"] >= 1
            assert detail["monitoring"]["drift_capable"] is True

    asyncio.run(_go())
    try:
        asyncio.run(_drain_background())
    except RuntimeError:
        pass


def test_v66_device_matrix():
    from app.services.devices import resolve_device

    # torch option resolves honestly (torch-CPU here, cuda/mps elsewhere)
    if TORCH_HERE:
        dev = resolve_device("torch")
        assert dev["backend"] == "torch"
        assert dev["resolved"] == "cpu"  # no accelerator in this sandbox
        assert "torch" in dev["note"].lower()
        auto = resolve_device("auto")
        assert auto["backend"] in ("numpy", "torch")

    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"dv-{tag}", 1)
            h = _auth(user["token"])
            res = await client.get("/ops/devices", headers=h)
            inv = res.json()
            assert inv["torch_installed"] == TORCH_HERE
            assert "torch" in " ".join(inv["allowed_modes"])
            if TORCH_HERE:
                assert "torch" in inv["compute_backend"]
                assert any("torch" in n for n in inv["notes"])

    asyncio.run(_go())
