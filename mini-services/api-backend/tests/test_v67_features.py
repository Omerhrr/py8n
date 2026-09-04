"""V67 feature tests: larger-context torch models, builder architecture
layers (staging / dead-letter), and the deployment surface that completes
the compose -> build -> train -> deploy -> operate platform loop.

- larger context: lm_train accepts n_ctx up to 512 on the torch backend
  (the numpy core honestly refuses TRAINING beyond 64 with torch guidance);
  gradient accumulation is a torch-only feature (numpy refuses loudly);
  generation is no longer clipped to the model's context - the window
  SLIDES (tokens_generated can exceed n_ctx, window_slid recorded).
- architecture layers: the builder grows staging_layer + dead_letter_queue
  components - raw rows land in {name} staging, the curated write runs a
  dead_letter-mode contract that QUARANTINES violating rows into
  {name} dead letter stamped with _dl_reasons/_dl_at, and the system
  detail derives the medallion layer map from the bound graphs.
- deployments: POST /deployments generates a LIVE serving workflow
  (webhook -> lm_generate for LMs, split_out -> model_predict for the
  tabular surface), the webhook answers synchronously with the model's
  output, stats derive from the execution log, toggle/delete/ownership
  behave, and GET /platform reads the five verbs off the estate.

Runs the FastAPI app in-process via httpx ASGITransport (same harness as v4-v66).
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
        "email": f"v67-{tag}-u{n}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v67 u{n} {tag}",
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


async def _run_and_wait(client: httpx.AsyncClient, wf_id: str, headers: dict, payload: dict | None = None) -> dict:
    res = await client.post(f"/workflows/{wf_id}/run", headers=headers, json={"payload": payload or {}})
    assert res.status_code in (200, 202), res.text
    exec_id = res.json()["execution_id"]
    for _ in range(400):
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


_CORPUS_SENTENCE = "the support agent resolved the ticket and the customer left a happy review"


def _corpus_rows(n: int = 14) -> list[dict]:
    return [{"doc": _CORPUS_SENTENCE + f" number {i} about the ticket and the agent"} for i in range(n)]


# ---------------------------------------------------------------------------
# 1) larger context: torch trains at ctx 128, numpy refuses honestly, and
#    generation slides past the context window on every backend.
# ---------------------------------------------------------------------------
def test_v67_larger_context_and_sliding_generation():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"ctx-{tag}", 1)
            h = _auth(user["token"])

            # ---- a) numpy honestly refuses TRAINING beyond 64 tokens ----
            graph = {"nodes": [
                _node("t", "manual_trigger"),
                _node("lm", "lm_train", {"text_column": "doc", "n_ctx": 128, "device": "cpu",
                                         "epochs": 2, "model_name": f"lm-refused-{tag}"}),
            ], "edges": [_edge("e1", "t", "lm")]}
            res = await client.post("/workflows", headers=h, json={"name": f"lm-refused-{tag}", "graph": graph})
            wf = res.json()
            run = await _run_and_wait(client, wf["id"], h, {"items": _corpus_rows()})
            assert run["status"] == "error"
            assert "torch backend" in (run.get("error") or ""), run.get("error")

            # ---- b) grad_accum is torch-only (numpy refuses loudly) ----
            graph = {"nodes": [
                _node("t", "manual_trigger"),
                _node("lm", "lm_train", {"text_column": "doc", "grad_accum": 2, "device": "cpu",
                                         "epochs": 2, "model_name": f"lm-ga-{tag}"}),
            ], "edges": [_edge("e1", "t", "lm")]}
            res = await client.post("/workflows", headers=h, json={"name": f"lm-ga-{tag}", "graph": graph})
            wf = res.json()
            run = await _run_and_wait(client, wf["id"], h, {"items": _corpus_rows()})
            assert run["status"] == "error"
            assert "grad_accum" in (run.get("error") or ""), run.get("error")

            # ---- c) torch backend trains at n_ctx=128 ----
            if TORCH_HERE:
                graph = {"nodes": [
                    _node("t", "manual_trigger"),
                    _node("lm", "lm_train", {"text_column": "doc", "n_ctx": 128, "device": "torch",
                                             "d_model": 16, "n_heads": 2, "epochs": 3,
                                             "grad_accum": 2,
                                             "model_name": f"lm-ctx-{tag}"}),
                ], "edges": [_edge("e1", "t", "lm")]}
                res = await client.post("/workflows", headers=h, json={"name": f"lm-ctx-{tag}", "graph": graph})
                wf = res.json()
                run = await _run_and_wait(client, wf["id"], h, {"items": _corpus_rows()})
                assert run["status"] == "success", str(run.get("error"))[:400]
                out = _node_run(run, "lm")["output"]
                assert out["metrics"]["context_length"] == 128
                assert out["metrics"]["device_backend"] == "torch"
                assert out["metrics"].get("grad_accum") == 2
                assert out["perplexity"] > 0

                # ---- d) generation beyond the context: the window slides ----
                res = await client.post("/workflows", headers=h, json={"name": f"lm-gen-{tag}", "graph": {
                    "nodes": [
                        _node("t", "manual_trigger"),
                        _node("gen", "lm_generate", {"model": f"lm-ctx-{tag}", "prompt": "the agent",
                                                     "max_tokens": 24, "device": "cpu"}),
                    ],
                    "edges": [_edge("e1", "t", "gen")],
                }})
                wf2 = res.json()
                run2 = await _run_and_wait(client, wf2["id"], h, {})
                assert run2["status"] == "success", str(run2.get("error"))[:400]
                gen = _node_run(run2, "gen")["output"]
                assert gen["tokens_generated"] == 24
                assert gen["context_window"] == 128
                assert gen["text"].strip(), "generation produced empty text"

            # ---- e) numpy SERVING of sliding generation: 32 tokens at ctx 16 ----
            graph = {"nodes": [
                _node("t", "manual_trigger"),
                _node("lm", "lm_train", {"text_column": "doc", "n_ctx": 16, "device": "cpu",
                                         "d_model": 16, "epochs": 3, "model_name": f"lm-small-{tag}"}),
            ], "edges": [_edge("e1", "t", "lm")]}
            res = await client.post("/workflows", headers=h, json={"name": f"lm-small-{tag}", "graph": graph})
            wf = res.json()
            run = await _run_and_wait(client, wf["id"], h, {"items": _corpus_rows()})
            assert run["status"] == "success", str(run.get("error"))[:400]
            res = await client.post("/workflows", headers=h, json={"name": f"lm-slide-{tag}", "graph": {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("gen", "lm_generate", {"model": f"lm-small-{tag}", "prompt": "the ticket",
                                                 "max_tokens": 32, "device": "cpu"}),
                ],
                "edges": [_edge("e1", "t", "gen")],
            }})
            wf2 = res.json()
            run2 = await _run_and_wait(client, wf2["id"], h, {})
            assert run2["status"] == "success", str(run2.get("error"))[:400]
            gen = _node_run(run2, "gen")["output"]
            assert gen["tokens_generated"] == 32, gen
            assert gen["context_window"] == 16
            assert gen["window_slid"] is True, "32 generated tokens at ctx 16 must mark the slide"

    asyncio.run(_go())
    asyncio.run(_drain_background())


# ---------------------------------------------------------------------------
# 2) builder architecture layers: staging lands raw rows, the curated write
#    dead-letters contract violations, and the system detail derives the
#    medallion map.
# ---------------------------------------------------------------------------
def test_v67_builder_staging_dead_letter_e2e():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"builder-{tag}", 1)
            h = _auth(user["token"])

            res = await client.post("/builder/systems", headers=h, json={
                "description": "Land raw invoice rows into a staging area with a schema contract, "
                               "quarantine bad rows in a dead letter queue, and keep the clean rows "
                               "in the curated table."})
            assert res.status_code == 201, res.text
            draft = res.json()
            spec = draft["spec"]
            picked = {c["id"] for c in spec["components"] if c["selected"]}
            assert {"staging_layer", "dead_letter_queue", "schema_contract"} <= picked, picked

            # answer the fields question so the contract has columns
            fields_q = next(q for q in spec["questions"] if q["key"] == "fields")
            assert fields_q["answered"] is False
            res = await client.post(f"/builder/systems/{draft['id']}/answers", headers=h, json={
                "answers": {"fields": "amount:number,product:text"}})
            assert res.status_code == 200, res.text
            spec = res.json()["spec"]
            assert spec["fields"] == [{"name": "amount", "dtype": "number"},
                                      {"name": "product", "dtype": "text"}], spec.get("fields")

            # the dependency rule: dead-letter without a contract is refused
            res = await client.post(f"/builder/systems/{draft['id']}/components", headers=h,
                                    json={"component_id": "dead_letter_queue", "selected": True})
            assert res.status_code == 200  # already selected, toggle is a no-op set
            # untick the schedule so the built graph uses a MANUAL trigger we
            # can fire with an inline payload (offline run, no cron needed)
            res = await client.post(f"/builder/systems/{draft['id']}/components", headers=h,
                                    json={"component_id": "schedule", "selected": False})
            assert res.status_code == 200, res.text

            res = await client.post(f"/builder/systems/{draft['id']}/build", headers=h,
                                    json={"as_system": True})
            assert res.status_code == 200, res.text
            built = res.json()["built"]
            assert built["on_violation"] == "dead_letter"
            assert built["staging_dataset_id"] and built["dead_letter_dataset_id"]
            assert set(built["layers"]) == {"staging", "curated", "dead_letter"}

            # the graph: trigger -> staging_write -> curated write (with DL target)
            res = await client.get(f"/workflows/{built['workflow_id']}", headers=h)
            graph = res.json()["graph"]
            writes = [n for n in graph["nodes"] if n["type"] == "dataset_write"]
            assert len(writes) == 2
            staging_node = next(n for n in writes if "staging" in (n["parameters"]["dataset"]).lower())
            curated_node = next(n for n in writes if "staging" not in (n["parameters"]["dataset"]).lower())
            assert staging_node["parameters"]["mode"] == "append"
            assert curated_node["parameters"]["dead_letter_dataset"] == built["dead_letter_dataset_name"]
            chain = [(e["source"], e["target"]) for e in graph["edges"]]
            assert ("trigger", "staging_write") in chain and ("staging_write", curated_node["id"]) in chain

            # RUN it offline: 2 valid rows + 1 dtype violation
            rows = [{"amount": 12, "product": "widget"},
                    {"amount": 5, "product": "gadget"},
                    {"amount": "abc", "product": "broken"}]
            res = await client.put(f"/workflows/{built['workflow_id']}", headers=h,
                                   json={"is_active": True})
            assert res.status_code in (200, 204), res.text
            run = await _run_and_wait(client, built["workflow_id"], h, {"items": rows})
            assert run["status"] == "success", str(run.get("error"))[:400]
            write_out = _node_run(run, curated_node["name"])["output"]
            assert write_out["written"] == 2
            assert write_out["dead_lettered"] == 1
            assert write_out["contract"]["on_violation"] == "dead_letter"

            # the curated dataset holds the passing rows; the DL dataset holds
            # the quarantined row, self-describing with _dl_reasons/_dl_at
            res = await client.get(f"/datasets/{built['dataset_id']}/rows", headers=h)
            assert [r["product"] for r in res.json()["rows"]] == ["widget", "gadget"]
            res = await client.get(f"/datasets/{built['dead_letter_dataset_id']}/rows", headers=h)
            dl_rows = res.json()["rows"]
            assert len(dl_rows) == 1
            assert dl_rows[0]["product"] == "broken"
            assert "amount:dtype" in dl_rows[0]["_dl_reasons"]
            assert dl_rows[0]["_dl_source"] == built["dataset_name"]
            assert dl_rows[0]["_dl_at"]

            # staging caught ALL three rows unmodified (the bronze copy)
            res = await client.get(f"/datasets/{built['staging_dataset_id']}/rows", headers=h)
            assert len(res.json()["rows"]) == 3

            # the system detail derives the medallion layer map
            res = await client.get(f"/systems/{built['system_id']}", headers=h)
            arch = res.json()["architecture"]
            assert arch["staging"] is True and arch["dead_letter"] is True
            layers = {l["layer"]: l["dataset"] for l in arch["layers"]}
            assert set(layers) == {"staging", "curated", "dead_letter"}

    asyncio.run(_go())
    asyncio.run(_drain_background())


# ---------------------------------------------------------------------------
# 3) deployments + platform: a trained model becomes a LIVE webhook endpoint
#    for both the LM and the tabular surface, and the five platform verbs
#    read back ready.
# ---------------------------------------------------------------------------
def test_v67_deployments_lm_tabular_and_platform():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"dep-{tag}", 1)
            h = _auth(user["token"])

            # ---- a) train a tiny LM through the real engine ----------------
            res = await client.post("/workflows", headers=h, json={"name": f"lm-dep-{tag}", "graph": {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("lm", "lm_train", {"text_column": "doc", "d_model": 16, "epochs": 3,
                                             "model_name": f"lm-dep-model-{tag}"}),
                ],
                "edges": [_edge("e1", "t", "lm")],
            }})
            wf = res.json()
            run = await _run_and_wait(client, wf["id"], h, {"items": _corpus_rows()})
            assert run["status"] == "success", str(run.get("error"))[:400]

            # ---- b) deploy it: py8n generates the LIVE serving workflow ----
            res = await client.post("/deployments", headers=h, json={
                "name": f"lm endpoint {tag}", "model": f"lm-dep-model-{tag}", "environment": "dev"})
            assert res.status_code == 201, res.text
            dep = res.json()
            assert dep["status"] == "live"
            assert dep["serving_mode"] == "generate"
            assert dep["workflow"]["is_active"] is True
            assert dep["model"]["name"] == f"lm-dep-model-{tag}"
            webhook_path = dep["workflow"]["webhook_path"]

            # ---- c) the endpoint answers SYNCHRONOUSLY over HTTP -----------
            res = await client.post(f"/webhooks/{dep['workflow']['id']}", json={"prompt": "the agent and the ticket"})
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["status"] == "success"
            last = body["last_output"]
            assert last["tokens_generated"] >= 1
            assert last["text"].strip()
            assert last["model"]["name"] == f"lm-dep-model-{tag}"

            # serving stats derive from the execution log
            res = await client.get(f"/deployments/{dep['id']}", headers=h)
            assert res.json()["stats"]["runs_7d"] >= 1
            assert res.json()["stats"]["last_call_status"] == "success"

            # ---- d) tabular deployment: split_out -> model_predict ---------
            rows = [{"a": float(i), "b": float((i * 7) % 11), "label": "yes" if i % 3 == 0 else "no"}
                    for i in range(32)]
            res = await client.post("/datasets", headers=h, json={"name": f"dep-rows-{tag}", "rows": rows})
            ds_name = res.json()["name"]
            res = await client.post("/workflows", headers=h, json={"name": f"nn-dep-{tag}", "graph": {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("r", "dataset_read", {"dataset": ds_name}),
                    _node("nn", "neural_train", {"task": "classification", "target": "label",
                                                 "features": "a,b", "hidden_layers": "8",
                                                 "epochs": 40, "learning_rate": 0.05,
                                                 "model_name": f"nn-dep-model-{tag}"}),
                ],
                "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "nn")],
            }})
            wf = res.json()
            run = await _run_and_wait(client, wf["id"], h, {})
            assert run["status"] == "success", str(run.get("error"))[:400]

            res = await client.post("/deployments", headers=h, json={
                "name": f"nn endpoint {tag}", "model": f"nn-dep-model-{tag}", "environment": "prod"})
            assert res.status_code == 201, res.text
            dep_nn = res.json()
            assert dep_nn["serving_mode"] == "predict"
            nn_path = dep_nn["workflow"]["id"]
            res = await client.post(f"/webhooks/{nn_path}", json={"rows": [{"a": 1.0, "b": 2.0}, {"a": 3.0, "b": 4.0}]})
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["status"] == "success"
            scored = body["last_output"]["items"]
            assert len(scored) == 2
            assert all("prediction" in r for r in scored)
            assert scored[0]["prediction"] in ("yes", "no")

            # unknown model refused honestly
            res = await client.post("/deployments", headers=h, json={
                "name": f"ghost {tag}", "model": f"no-such-model-{tag}"})
            assert res.status_code == 400
            assert "not found" in res.json()["detail"]

            # ---- e) toggle / ownership / retire -----------------------------
            res = await client.post(f"/deployments/{dep['id']}/toggle", headers=h)
            assert res.json()["status"] == "disabled"
            assert res.json()["workflow"]["is_active"] is False
            res = await client.post(f"/deployments/{dep['id']}/toggle", headers=h)
            assert res.json()["status"] == "live"

            user_b = await _mk_user(client, f"dep-{tag}", 2)
            res = await client.get(f"/deployments/{dep['id']}", headers=_auth(user_b["token"]))
            assert res.status_code == 404
            res = await client.delete(f"/deployments/{dep['id']}", headers=_auth(user_b["token"]))
            assert res.status_code == 404

            res = await client.delete(f"/deployments/{dep['id']}", headers=h)
            assert res.status_code == 200
            assert res.json()["workflow_deactivated"] is True
            res = await client.get(f"/deployments/{dep['id']}", headers=h)
            assert res.status_code == 404
            res = await client.get(f"/workflows/{dep['workflow']['id']}", headers=h)
            assert res.json()["is_active"] is False  # the workflow survives, deactivated

            # ---- f) compose + build for the same user, then the platform ----
            res = await client.post("/systems", headers=h, json={"name": f"dep system {tag}"})
            assert res.status_code == 201, res.text
            sysrow = res.json()
            res = await client.post(f"/systems/{sysrow['id']}/components", headers=h,
                                    json={"kind": "workflow", "ref_id": dep_nn["workflow"]["id"]})
            assert res.status_code in (201, 200), res.text

            res = await client.post("/builder/systems", headers=h, json={
                "description": "collect invoices from a csv upload every day"})
            assert res.status_code == 201, res.text
            draft = res.json()
            res = await client.post(f"/builder/systems/{draft['id']}/build", headers=h, json={})
            assert res.status_code == 200, res.text

            res = await client.get("/platform", headers=h)
            assert res.status_code == 200, res.text
            platform = res.json()
            assert platform["vision"].startswith("A platform for composing")
            for verb in ("composing", "building", "training", "deploying", "operating"):
                assert verb in platform["verdicts"], platform["verdicts"]
            assert all(platform["verdicts"].values()), platform["verdicts"]
            assert platform["ready"] is True
            assert platform["deploying"]["deployments"] >= 1  # the nn deployment is still live
            assert platform["deploying"]["serving_invocations_7d"] >= 1

    asyncio.run(_go())
    asyncio.run(_drain_background())
