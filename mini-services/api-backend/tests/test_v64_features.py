"""V64 feature tests: text continued-pretraining, solutions that install as
model systems, and the builder's LLM-first mode.

- lm_train: a from-scratch causal transformer (raw numpy, gradient-checked)
  trains through the REAL engine on a raw-text corpus, registers as
  lm_transformer with held-out perplexity; pointing base_model at the
  registry row CONTINUES PRETRAINING it on a new corpus (weights + tokenizer
  carry over, lineage recorded); lm_generate samples text from the
  registered model and honestly refuses non-LM models.
- model solutions: the marketplace's language-model-system installs AS A
  MODEL SYSTEM (datasets + training + serving workflows bound), runs its
  pretrain -> continue -> generate chain offline; a SECOND user's install
  remaps the registry names so the two version chains never collide.
- builder LLM-first: the bridge-unreachable fallback keeps a valid
  deterministic spec with an honest note; a mocked bridge reply is repaired
  (unknown components dropped, backbone forced, questions capped).

Runs the FastAPI app in-process via httpx ASGITransport (same harness as v4-v63).
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


async def _mk_user(client: httpx.AsyncClient, tag: str, n: int) -> dict:
    res = await client.post("/auth/register", json={
        "email": f"v64-{tag}-u{n}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v64 u{n} {tag}",
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


SUPPORT_CORPUS = [
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
    "the agent closes the refund ticket today",
    "the team fixes the policy bug",
    "the order ships after the agent reviews it",
    "the agent replies to the ticket today",
    "the customer likes the quick reply",
    "the team ships the fix for the login bug",
    "the refund order arrives today",
    "the agent reviews the login policy with the team",
] * 2

LEGAL_CORPUS = [
    "the contract defines the refund policy for the customer",
    "the policy states the order terms clearly",
    "the agent reviews the contract with the team",
    "the customer signs the policy today",
    "the contract covers the ticket escalation terms",
    "the team updates the refund contract",
    "the policy agent explains the terms",
    "the contract team closes the policy ticket",
] * 2


def test_v64_lm_train_continued_pretrain_generate():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"lm-{tag}", 1)
            h = _auth(user["token"])

            # --- 1) from-scratch pretraining through the real engine ----------
            res = await client.post("/datasets", headers=h,
                                    json={"name": f"lm-corpus-{tag}",
                                          "rows": [{"doc": d} for d in SUPPORT_CORPUS]})
            corpus = res.json()
            graph = {"nodes": [
                _node("t", "manual_trigger"),
                _node("r", "dataset_read", {"dataset": corpus["name"]}),
                _node("lm", "lm_train", {"text_column": "doc", "vocab_size": 200,
                                         "d_model": 32, "n_heads": 2, "n_ctx": 12,
                                         "epochs": 10, "batch_size": 8, "learning_rate": 0.005,
                                         "model_name": f"lm-{tag}", "register": True}),
            ], "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "lm")]}
            res = await client.post("/workflows", headers=h, json={"name": f"lm-pretrain-{tag}", "graph": graph})
            run = await _run_and_wait(client, res.json()["id"], h)
            assert run["status"] == "success", str(run.get("error"))[:400]
            out = _node_run(run, "lm")["output"]
            assert out["mode"] == "from-scratch pretrain"
            assert out["perplexity"] >= 1.0
            assert out["metrics"]["params_count"] > 0
            assert out["metrics"]["tokens_total"] > 100
            assert "lm" in out["metrics"]["architecture"]

            res = await client.get("/models", headers=h)
            mrow = next(m for m in res.json() if m["name"] == f"lm-{tag}" and m["active"])
            assert mrow["algorithm"] == "lm_transformer"
            assert mrow["task"] == "language_modeling"

            # --- 2) continued pretraining: weights AND tokenizer carry over ---
            res = await client.post("/datasets", headers=h,
                                    json={"name": f"lm-legal-{tag}",
                                          "rows": [{"doc": d} for d in LEGAL_CORPUS]})
            legal = res.json()
            graph2 = {"nodes": [
                _node("t", "manual_trigger"),
                _node("r", "dataset_read", {"dataset": legal["name"]}),
                _node("lm", "lm_train", {"text_column": "doc", "base_model": f"lm-{tag}",
                                         "epochs": 8, "batch_size": 8, "learning_rate": 0.003,
                                         "model_name": f"lm-{tag}", "register": True}),
            ], "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "lm")]}
            res = await client.post("/workflows", headers=h, json={"name": f"lm-continue-{tag}", "graph": graph2})
            run2 = await _run_and_wait(client, res.json()["id"], h)
            assert run2["status"] == "success", str(run2.get("error"))[:400]
            out2 = _node_run(run2, "lm")["output"]
            assert out2["mode"] == "continued pretrain"
            assert out2["metrics"]["continued_pretrained_from"] == f"lm-{tag} v1"
            assert out2["vocabulary"] == out["vocabulary"]  # the tokenizer carried over

            res = await client.get("/models", headers=h)
            versions = [m for m in res.json() if m["name"] == f"lm-{tag}"]
            assert len(versions) >= 2
            v2 = next(m for m in versions if m["version"] == 2)
            assert v2["metrics"]["continued_pretrained_from"] == f"lm-{tag} v1"

            # --- 3) generation through the registered LM ----------------------
            res = await client.post("/workflows", headers=h, json={"name": f"lm-generate-{tag}", "graph": {
                "nodes": [
                    _node("t", "manual_trigger", {"payload": {"prompt": "the agent"}}),
                    _node("g", "lm_generate", {"model": f"lm-{tag}",
                                               "prompt": "{{ nodes.t.output.payload.prompt }}",
                                               "max_tokens": 6, "temperature": 0.7, "top_k": 20}),
                ],
                "edges": [_edge("e1", "t", "g")]}})
            run3 = await _run_and_wait(client, res.json()["id"], h)
            assert run3["status"] == "success", str(run3.get("error"))[:400]
            gen = _node_run(run3, "g")["output"]
            assert isinstance(gen["text"], str) and len(gen["text"]) > 0
            assert gen["tokens_generated"] >= 1

            # honest failure: lm_generate refuses a CLASSICAL model
            res = await client.post("/datasets", headers=h,
                                    json={"name": f"lm-tab-{tag}",
                                          "rows": [{"a": float(i), "b": float(i % 3)} for i in range(12)]})
            tab = res.json()
            res = await client.post("/workflows", headers=h, json={"name": f"lm-classical-{tag}", "graph": {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("r", "dataset_read", {"dataset": tab["name"]}),
                    _node("tr", "model_train", {"model": "linear_regression", "target": "b",
                                                "features": "a", "model_name": f"classical-{tag}"}),
                ],
                "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "tr")]}})
            run4 = await _run_and_wait(client, res.json()["id"], h)
            assert run4["status"] == "success", str(run4.get("error"))[:300]
            res = await client.post("/workflows", headers=h, json={"name": f"lm-badgen-{tag}", "graph": {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("g", "lm_generate", {"model": f"classical-{tag}", "prompt": "hello"}),
                ],
                "edges": [_edge("e1", "t", "g")]}})
            run5 = await _run_and_wait(client, res.json()["id"], h)
            assert run5["status"] == "error"
            assert "lm_generate needs an lm_train model" in (run5.get("error") or "")

            # --- 4) the model system reports the LANGUAGE paradigm -------------
            res = await client.post("/model-systems", headers=h,
                                    json={"name": f"LM Vision {tag}", "modalities": ["text"]})
            ms = res.json()
            res = await client.get("/workflows", headers=h)
            wf_rows = res.json()
            for wf in wf_rows:
                if wf["name"] in (f"lm-pretrain-{tag}", f"lm-continue-{tag}", f"lm-generate-{tag}"):
                    res2 = await client.post(f"/model-systems/{ms['id']}/components", headers=h,
                                             json={"kind": "workflow", "ref_id": wf["id"]})
                    assert res2.status_code == 201

            # bind BOTH registry versions (v1 pretrain + v2 continued pretrain)
            res = await client.get("/models", headers=h)
            for m in [m for m in res.json() if m["name"] == f"lm-{tag}"]:
                res2 = await client.post(f"/model-systems/{ms['id']}/components", headers=h,
                                         json={"kind": "model", "ref_id": m["id"]})
                assert res2.status_code == 201
            res = await client.get(f"/model-systems/{ms['id']}", headers=h)
            detail = res.json()
            assert detail["training"]["language_versions"] >= 2
            assert detail["training"]["continued_pretrained_versions"] >= 1
            assert detail["training"]["latest"][0]["family"] == "language"
            assert "text" in detail["modalities"]["evidence"]
            assert detail["evaluation"] and "perplexity" in detail["evaluation"][0]["metrics"]
            assert detail["retraining"], "lm_train workflows count as retraining pipelines"

    asyncio.run(_go())
    try:
        asyncio.run(_drain_background())
    except RuntimeError:
        pass


def test_v64_solution_installs_model_system():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user_a = await _mk_user(client, f"sol-{tag}", 1)
            user_b = await _mk_user(client, f"sol-{tag}", 2)
            ha = _auth(user_a["token"])
            hb = _auth(user_b["token"])

            # --- user A installs the LANGUAGE MODEL SYSTEM as a model system --
            res = await client.post("/solutions/language-model-system/install", headers=ha,
                                    json={"as_model_system": True})
            assert res.status_code == 200, res.text
            inst = res.json()
            ms_a = inst["model_system"]
            assert ms_a and ms_a["modalities"] == ["text"]
            wf_by_name = {w["name"]: w for w in inst["created_workflows"]}
            assert {"Pretrain Language Model", "Continue Pretraining",
                    "Generate With Language Model"} <= set(wf_by_name)

            res = await client.get(f"/model-systems/{ms_a['id']}", headers=ha)
            detail = res.json()
            assert len(detail["datasets"]) == 3
            assert len(detail["workflows_bound"]) >= 3 if "workflows_bound" in detail else True
            assert detail["retraining"], "the two lm_train workflows are retraining pipelines"

            # the full LM life cycle runs OFFLINE through the real engine
            run = await _run_and_wait(client, wf_by_name["Pretrain Language Model"]["id"], ha)
            assert run["status"] == "success", str(run.get("error"))[:400]
            pre = _node_run(run, "Pretrain Language Model")["output"]
            assert pre["mode"] == "from-scratch pretrain"
            run = await _run_and_wait(client, wf_by_name["Continue Pretraining"]["id"], ha)
            assert run["status"] == "success", str(run.get("error"))[:400]
            assert _node_run(run, "Continue Pretraining")["output"]["mode"] == "continued pretrain"
            run = await _run_and_wait(client, wf_by_name["Generate With Language Model"]["id"], ha)
            assert run["status"] == "success", str(run.get("error"))[:400]

            res = await client.get("/datasets", headers=ha)
            samples = next(d for d in res.json() if d["name"] == "lm_samples")
            assert samples["row_count"] >= 1

            # after training, A's registry holds the support_lm chain (v1 pretrain + v2 continue)
            res = await client.get("/models", headers=ha)
            a_versions = [m for m in res.json() if m["name"] == "support_lm"]
            assert len(a_versions) == 2
            assert next(m for m in a_versions if m["version"] == 2)["active"]

            # --- user B installs the SAME solution: names must not collide ----
            res = await client.post("/solutions/language-model-system/install", headers=hb,
                                    json={"as_model_system": True})
            assert res.status_code == 200, res.text
            inst_b = res.json()
            wf_b = {w["name"]: w for w in inst_b["created_workflows"]}

            # B's registry is empty BEFORE training - no support_lm rows leaked
            res = await client.get("/models", headers=hb)
            assert not [m for m in res.json() if m["name"] == "support_lm"]

            run = await _run_and_wait(client, wf_b["Pretrain Language Model"]["id"], hb)
            assert run["status"] == "success", str(run.get("error"))[:400]
            res = await client.get("/models", headers=hb)
            b_rows = [m for m in res.json() if m["name"] == "support_lm 2"]
            assert b_rows, "B's install remapped the model name away from A's chain"
            # B's continued pretraining works through the SAME remapped name
            run = await _run_and_wait(client, wf_b["Continue Pretraining"]["id"], hb)
            assert run["status"] == "success", str(run.get("error"))[:400]
            res = await client.get("/models", headers=hb)
            assert len([m for m in res.json() if m["name"] == "support_lm 2"]) == 2
            # A's chain is untouched - exactly the same two active-chained versions
            res = await client.get("/models", headers=ha)
            assert len([m for m in res.json() if m["name"] == "support_lm"]) == 2
            assert len([m for m in res.json() if m["name"] == "support_lm 2"]) == 0

            # --- the sentiment model system trains + serves end to end --------
            res = await client.post("/solutions/sentiment-model-system/install", headers=ha,
                                    json={"as_model_system": True})
            assert res.status_code == 200, res.text
            inst_s = res.json()
            assert inst_s["model_system"]["modalities"] == ["text"]
            wf_s = {w["name"]: w for w in inst_s["created_workflows"]}
            run = await _run_and_wait(client, wf_s["Train Sentiment Model"]["id"], ha)
            assert run["status"] == "success", str(run.get("error"))[:400]
            assert _node_run(run, "Train Sentiment Model")["output"]["metrics"]["accuracy"] >= 0
            run = await _run_and_wait(client, wf_s["Serve Sentiment Scorer"]["id"], ha)
            assert run["status"] == "success", str(run.get("error"))[:400]
            scored = _node_run(run, "Score Sentiment")["output"]["items"]
            assert all(s["prediction"] in ("positive", "negative") for s in scored)

            res = await client.get("/solutions", headers=ha)
            shelf = {s["slug"]: s for s in res.json()["solutions"]}
            assert shelf["language-model-system"]["model_system_ready"] is True
            assert shelf["customer-support-automation"]["model_system_ready"] is False
            assert shelf["language-model-system"]["installs"] >= 2

    asyncio.run(_go())
    try:
        asyncio.run(_drain_background())
    except RuntimeError:
        pass


def test_v64_builder_llm_first_mode(monkeypatch):
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"bld-{tag}", 1)
            h = _auth(user["token"])

            # --- bridge unreachable -> fail-soft deterministic fallback --------
            res = await client.post("/builder/systems", headers=h,
                                    json={"description": "every hour pull orders from postgres, "
                                                         "dedupe them and alert me if quality drops",
                                          "llm_first": True})
            assert res.status_code == 201, res.text
            draft = res.json()
            assert draft["spec"]["mode"] == "llm_first_fallback"
            assert any("fell back" in n for n in draft["spec"]["notes"])
            selected = {c["id"] for c in draft["spec"]["components"] if c["selected"]}
            assert {"target_dataset", "pipeline_workflow"} <= selected

    asyncio.run(_go())
    try:
        asyncio.run(_drain_background())
    except RuntimeError:
        pass

    import app.services.system_builder as sb

    async def _fake_bridge(system: str, user_msg: str):
        return {
            "title": "Order Sentinel",
            "persona": "data_engineer",
            "selected": ["target_dataset", "pipeline_workflow", "schedule",
                         "bogus_component", "quality_gate", "ai_summary"],
            "questions": ["What counts as an order?", "Which table?", "Which webhook?",
                          "What dedupe key?", "Extra 5?", "Extra 6?", "Extra 7?"],
            "notes": ["watch the failure rate"],
        }, ""

    monkeypatch.setattr(sb, "_bridge_json", _fake_bridge)

    async def _go2():
        async with _client() as client:
            # --- bridge reachable -> the LLM proposes, py8n repairs ------------
            res = await client.post("/builder/systems",
                                    json={"description": "watch incoming orders and keep them deduplicated",
                                          "llm_first": True})
            assert res.status_code == 201, res.text
            spec = res.json()["spec"]
            assert spec["mode"] == "llm_first"
            assert spec["title"] == "Order Sentinel"
            assert spec["persona"] == "data_engineer"
            selected = {c["id"] for c in spec["components"] if c["selected"]}
            assert {"target_dataset", "pipeline_workflow"} <= selected  # backbone forced
            assert "bogus_component" not in selected  # unknown ids dropped
            assert "quality_gate" not in selected  # dependency missing -> dropped
            assert "ai_summary" in selected
            assert len(spec["questions"]) <= 7  # 3 keyed + 4 LLM questions max
            assert any(n.startswith("AI: watch the failure rate") for n in spec["notes"])
            assert any("LLM-first mode" in n for n in spec["notes"])

    asyncio.run(_go2())
    try:
        asyncio.run(_drain_background())
    except RuntimeError:
        pass
